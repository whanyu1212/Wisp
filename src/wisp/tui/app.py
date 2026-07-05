"""Minimal Rich-based TUI shell for Wisp.

This is intentionally small: it provides an interactive terminal shell that uses
`RpcController` rather than reaching into CLI internals. A future full-screen TUI
can replace the rendering layer while keeping this controller-facing flow.
"""

from __future__ import annotations

import os
import sys
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

import anyio
from anyio.streams.memory import MemoryObjectSendStream
from rich.console import Console

from wisp.config import WispConfig
from wisp.events import (
    AssistantMessage,
    ErrorEvent,
    KnownWispEvent,
    RpcCommandFinished,
    SessionSaved,
    TokenDelta,
    ToolApprovalRequested,
)
from wisp.rpc import JsonlSubprocessRpcTransport, RpcController
from wisp.runtime.extensions import build_runtime
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tui.live import LiveFullscreenInputInterrupted, LiveFullscreenTui
from wisp.tui.rendering import (
    TuiRenderer,
    TuiRendererKind,
    TuiViewSnapshot,
    create_tui_renderer,
)


@dataclass(frozen=True)
class TuiOptions:
    """Options used to start the Wisp TUI shell."""

    config: WispConfig
    allow_read_tools: bool = False
    allowed_tools: tuple[str, ...] = ()
    resume: str | None = None
    continue_latest: bool = False
    approve_unsafe_tools: bool = False
    max_tool_iterations: int | None = None
    renderer: TuiRendererKind = TuiRendererKind.line


class TuiStatus(StrEnum):
    """High-level TUI interaction state."""

    idle = "idle"
    running = "running"
    waiting_for_approval = "waiting_for_approval"
    exiting = "exiting"


@dataclass
class TuiInteractionState:
    """Mutable interaction state for the minimal TUI event loop."""

    status: TuiStatus = TuiStatus.idle
    current_command_id: str | None = None
    shutdown_command_id: str | None = None
    pending_approval: ToolApprovalRequested | None = None
    queued_prompts: deque[str] = field(default_factory=deque)
    exit_requested: bool = False
    input_closed: bool = False
    cancel_requested: bool = False
    token_stream_started: bool = False
    rendered_tokens: bool = False


@dataclass
class TuiViewState:
    """Shell-owned renderer-visible TUI state."""

    status: str = "idle"
    input_hint: str = "wisp> "
    input_mode: str = "idle"
    queued_follow_ups: int = 0
    last_session: str | None = None

    def snapshot(self) -> TuiViewSnapshot:
        """Return an immutable renderer-facing view snapshot."""

        return TuiViewSnapshot(
            status=self.status,
            input_hint=self.input_hint,
            input_mode=self.input_mode,
            queued_follow_ups=self.queued_follow_ups,
            last_session=self.last_session,
        )


class TuiController(Protocol):
    """Controller surface consumed by the TUI shell."""

    async def prompt(self, prompt: str, *, command_id: str | None = None) -> str: ...

    async def cancel(self, target_id: str, *, command_id: str | None = None) -> str: ...

    async def approve(
        self,
        call_id: str,
        *,
        approved: bool = True,
        reason: str | None = None,
        command_id: str | None = None,
    ) -> str: ...

    async def shutdown(self, *, command_id: str | None = None) -> str: ...

    def events(self) -> AsyncIterator[KnownWispEvent]: ...

    async def close(self) -> None: ...


PromptReader = Callable[[str], Awaitable[str]]


class _InputMode(StrEnum):
    idle = "idle"
    running = "running"
    approval = "approval"
    exiting = "exiting"


@dataclass(frozen=True)
class _InputLine:
    text: str
    mode: _InputMode


@dataclass(frozen=True)
class _InputClosed:
    mode: _InputMode


@dataclass(frozen=True)
class _InputInterrupted:
    mode: _InputMode


@dataclass(frozen=True)
class _RpcEvent:
    event: KnownWispEvent


@dataclass(frozen=True)
class _RpcEventsClosed:
    error: str | None = None


type _TuiSignal = _InputLine | _InputClosed | _InputInterrupted | _RpcEvent | _RpcEventsClosed


async def run_tui(
    options: TuiOptions,
    *,
    console: Console | None = None,
    prompt_reader: PromptReader | None = None,
    controller: TuiController | None = None,
) -> None:
    """Run the minimal Wisp TUI shell."""

    selected_console = console or Console()
    selected_controller = controller
    owns_controller = selected_controller is None
    if selected_controller is None:
        await _preflight_tui_options(options)
        transport = await JsonlSubprocessRpcTransport.start(_rpc_command(options), env=_rpc_env())
        selected_controller = RpcController(transport)

    live_tui: LiveFullscreenTui | None = None
    selected_renderer = create_tui_renderer(options.renderer, selected_console)
    selected_prompt_reader = prompt_reader
    if (
        options.renderer is TuiRendererKind.fullscreen
        and prompt_reader is None
        and console is None
        and _stdio_is_interactive()
    ):
        live_tui = LiveFullscreenTui()
        selected_renderer = live_tui
        selected_prompt_reader = live_tui.read_prompt

    shell = TuiShell(
        selected_controller,
        renderer=selected_renderer,
        prompt_reader=selected_prompt_reader,
    )
    try:
        await shell.run()
    finally:
        try:
            if live_tui is not None:
                await live_tui.close()
        finally:
            if owns_controller:
                await selected_controller.close()


class TuiShell:
    """Small prompt/event shell that drives Wisp through `RpcController`."""

    def __init__(
        self,
        controller: TuiController,
        *,
        console: Console | None = None,
        prompt_reader: PromptReader | None = None,
        state: TuiInteractionState | None = None,
        renderer: TuiRenderer | None = None,
    ) -> None:
        self.controller = controller
        self.renderer = (
            renderer if renderer is not None else create_tui_renderer(TuiRendererKind.line, console)
        )
        self.prompt_reader = prompt_reader or _default_prompt_reader
        self.state = state or TuiInteractionState()
        self.view = TuiViewState()

    async def run(self) -> None:
        """Run the interactive prompt/event loop."""

        self.renderer.startup()
        self._sync_view()
        send, receive = anyio.create_memory_object_stream[_TuiSignal](100)
        async with anyio.create_task_group() as task_group, send, receive:
            task_group.start_soon(self._read_inputs, send.clone())
            task_group.start_soon(self._read_rpc_events, send.clone())
            while True:
                signal = await receive.receive()
                should_exit = await self._handle_signal(signal)
                if should_exit:
                    task_group.cancel_scope.cancel()
                    return

    def _sync_view(self) -> None:
        mode = _input_mode_for_status(self.state.status)
        self._update_view(
            status=_view_status_for_status(self.state.status),
            input_hint=_prompt_for_mode(mode),
            input_mode=mode,
            queued_follow_ups=len(self.state.queued_prompts),
        )

    def _update_view(
        self,
        *,
        status: str | None = None,
        input_hint: str | None = None,
        input_mode: _InputMode | None = None,
        queued_follow_ups: int | None = None,
        last_session: str | None = None,
    ) -> None:
        if status is not None:
            self.view.status = status
        if input_hint is not None:
            self.view.input_hint = input_hint
        if input_mode is not None:
            self.view.input_mode = input_mode.value
        if queued_follow_ups is not None:
            self.view.queued_follow_ups = queued_follow_ups
        if last_session is not None:
            self.view.last_session = last_session
        self.renderer.view_updated(self.view.snapshot())

    async def _read_inputs(self, send: MemoryObjectSendStream[_TuiSignal]) -> None:
        async with send:
            while True:
                mode = _input_mode_for_status(self.state.status)
                try:
                    text = (await self.prompt_reader(_prompt_for_mode(mode))).strip()
                except EOFError:
                    await send.send(_InputClosed(mode=self._submitted_input_mode(mode)))
                    return
                except (KeyboardInterrupt, LiveFullscreenInputInterrupted):
                    await send.send(_InputInterrupted(mode=self._submitted_input_mode(mode)))
                    continue
                await send.send(_InputLine(text=text, mode=self._submitted_input_mode(mode)))

    def _submitted_input_mode(self, requested_mode: _InputMode) -> _InputMode:
        consume_mode = getattr(self.renderer, "consume_submitted_input_mode", None)
        if callable(consume_mode):
            return _coerce_input_mode(consume_mode(requested_mode.value), fallback=requested_mode)
        return requested_mode

    async def _read_rpc_events(self, send: MemoryObjectSendStream[_TuiSignal]) -> None:
        async with send:
            try:
                async for event in self.controller.events():
                    await send.send(_RpcEvent(event=event))
            except Exception as exc:  # noqa: BLE001 - surface event reader failures in the TUI
                await send.send(_RpcEventsClosed(error=str(exc)))
            else:
                await send.send(_RpcEventsClosed())

    async def _handle_signal(self, signal: _TuiSignal) -> bool:
        if isinstance(signal, _InputLine):
            return await self._handle_input_line(signal)
        if isinstance(signal, _InputClosed):
            return await self._handle_input_closed(signal)
        if isinstance(signal, _InputInterrupted):
            return await self._handle_input_interrupted(signal)
        if isinstance(signal, _RpcEvent):
            return await self._handle_rpc_event(signal.event)
        return self._handle_rpc_closed(signal)

    async def _handle_input_line(self, signal: _InputLine) -> bool:
        text = signal.text
        if self.state.status is TuiStatus.exiting:
            return False
        if self.state.pending_approval is not None:
            if text in {"/quit", "/exit", ":q"}:
                return await self._handle_quit()
            if text == "/help":
                self._render_help()
                return False
            if signal.mode is _InputMode.approval:
                return await self._answer_pending_approval(text, exit_after_denial=False)
            if text and self.state.current_command_id is not None:
                self.state.queued_prompts.append(text)
                self._update_view(queued_follow_ups=len(self.state.queued_prompts))
                self.renderer.queued_follow_up(len(self.state.queued_prompts))
            return False
        if not text:
            return False
        if text in {"/quit", "/exit", ":q"}:
            return await self._handle_quit()
        if text == "/help":
            self._render_help()
            return False
        if self.state.current_command_id is not None:
            self.state.queued_prompts.append(text)
            self._update_view(queued_follow_ups=len(self.state.queued_prompts))
            self.renderer.queued_follow_up(len(self.state.queued_prompts))
            return False
        return await self._start_prompt(text)

    async def _handle_input_closed(self, signal: _InputClosed) -> bool:
        self.state.input_closed = True
        if self.state.status is TuiStatus.exiting:
            return False
        self.state.exit_requested = True
        if self.state.pending_approval is not None:
            # Denying the pending approval is the conservative safety behavior even
            # when a live renderer reports that EOF began under an older mode.
            return await self._answer_pending_approval(
                "",
                approved=False,
                reason="Denied from TUI: input closed",
                exit_after_denial=True,
            )
        if self.state.current_command_id is not None:
            self.state.queued_prompts.clear()
            self._update_view(queued_follow_ups=0)
            self.renderer.input_closed_finishing_prompt()
            return False
        return await self._request_shutdown()

    async def _handle_input_interrupted(self, signal: _InputInterrupted) -> bool:
        if self.state.pending_approval is not None:
            # Denying the pending approval is the conservative safety behavior even
            # when a live renderer reports that Ctrl-C began under an older mode.
            return await self._answer_pending_approval(
                "",
                approved=False,
                reason="Denied from TUI: interrupted",
                exit_after_denial=False,
            )
        if self.state.current_command_id is not None:
            return await self._cancel_current("Cancelling current prompt...")
        self.renderer.input_cleared()
        return False

    async def _handle_quit(self) -> bool:
        self.state.exit_requested = True
        self.state.queued_prompts.clear()
        self._update_view(queued_follow_ups=0)
        if self.state.pending_approval is not None:
            return await self._answer_pending_approval(
                "",
                approved=False,
                reason="Denied from TUI: quit requested",
                exit_after_denial=True,
            )
        if self.state.current_command_id is not None:
            return await self._cancel_current("Quit requested; cancelling current prompt...")
        return await self._request_shutdown()

    async def _start_prompt(self, prompt: str) -> bool:
        self.state.status = TuiStatus.running
        self.state.pending_approval = None
        self.state.cancel_requested = False
        self.state.token_stream_started = False
        self.state.rendered_tokens = False
        self._sync_view()
        self.renderer.prompt_submitted(prompt)
        self.renderer.running()
        try:
            command_id = await self.controller.prompt(prompt)
        except Exception as exc:
            self._update_view(status="error")
            self.renderer.send_failed("prompt", exc)
            return True
        self.state.current_command_id = command_id
        return False

    async def _cancel_current(self, message: str) -> bool:
        command_id = self.state.current_command_id
        if command_id is None:
            return False
        if self.state.cancel_requested:
            self.renderer.cancel_already_requested()
            return False
        self.state.queued_prompts.clear()
        self.state.cancel_requested = True
        self._update_view(status="cancelling", queued_follow_ups=0)
        self.renderer.cancelling(message)
        try:
            await self.controller.cancel(command_id)
        except Exception as exc:
            self._update_view(status="error")
            self.renderer.send_failed("cancel", exc)
            return True
        self.state.status = TuiStatus.running
        self.state.pending_approval = None
        return False

    async def _request_shutdown(self) -> bool:
        if self.state.shutdown_command_id is not None:
            self.state.status = TuiStatus.exiting
            self._sync_view()
            return False
        self.state.status = TuiStatus.exiting
        self._sync_view()
        try:
            shutdown_id = await self.controller.shutdown()
        except Exception as exc:
            self._update_view(status="error")
            self.renderer.shutdown_failed(exc)
            return True
        self.state.shutdown_command_id = shutdown_id
        return False

    async def _answer_pending_approval(
        self,
        answer: str,
        *,
        approved: bool | None = None,
        reason: str | None = None,
        exit_after_denial: bool,
    ) -> bool:
        approval = self.state.pending_approval
        if approval is None:
            return False
        selected_approved = (
            approved if approved is not None else answer.strip().lower() in {"y", "yes"}
        )
        selected_reason = None if selected_approved else reason or "Denied from TUI"
        if reason == "Denied from TUI: input closed":
            self.renderer.approval_input_closed()
        elif reason == "Denied from TUI: interrupted":
            self.renderer.approval_interrupted()
        elif reason == "Denied from TUI: quit requested":
            self.renderer.quit_requested_denying_approval()
        ok = await self._send_approval(
            approval.call_id,
            approved=selected_approved,
            reason=selected_reason,
        )
        self.state.pending_approval = None
        if not ok:
            return True
        self.state.status = TuiStatus.running if self.state.current_command_id else TuiStatus.idle
        if exit_after_denial and not selected_approved:
            self.state.exit_requested = True
        self._sync_view()
        return False

    async def _send_approval(
        self,
        call_id: str,
        *,
        approved: bool,
        reason: str | None,
    ) -> bool:
        try:
            await self.controller.approve(call_id, approved=approved, reason=reason)
        except Exception as exc:
            self._update_view(status="error")
            self.renderer.send_failed("approval", exc)
            return False
        return True

    async def _handle_rpc_event(self, event: KnownWispEvent) -> bool:
        if isinstance(event, TokenDelta):
            self.state.token_stream_started = True
            self.state.rendered_tokens = True
            self.renderer.token_delta(event.delta)
            return False
        if self.state.token_stream_started:
            self.renderer.end_token_stream()
            self.state.token_stream_started = False
        if isinstance(event, AssistantMessage) and self.state.rendered_tokens:
            return False
        if isinstance(event, ToolApprovalRequested):
            self.state.pending_approval = event
            self.state.status = TuiStatus.waiting_for_approval
            self._sync_view()
            self.renderer.approval_request(event)
            if self.state.input_closed:
                return await self._answer_pending_approval(
                    "",
                    approved=False,
                    reason="Denied from TUI: input closed",
                    exit_after_denial=True,
                )
            return False

        self._render_event(event)
        if isinstance(event, RpcCommandFinished):
            if event.command_id == self.state.shutdown_command_id:
                return True
            if event.command_id == self.state.current_command_id:
                return await self._finish_current_prompt(event)
        return False

    async def _finish_current_prompt(self, event: RpcCommandFinished) -> bool:
        was_cancelled = (not event.ok) and _is_rpc_cancelled_message(event.error)
        self.state.current_command_id = None
        self.state.pending_approval = None
        self.state.token_stream_started = False
        self.state.rendered_tokens = False
        if self.state.exit_requested or not event.ok:
            self.state.queued_prompts.clear()
        if self.state.exit_requested:
            self.state.cancel_requested = False
            return await self._request_shutdown()
        if self.state.queued_prompts:
            queued_prompt = self.state.queued_prompts.popleft()
            self._update_view(
                status="running queued follow-up",
                input_hint=_prompt_for_mode(_InputMode.running),
                input_mode=_InputMode.running,
                queued_follow_ups=len(self.state.queued_prompts),
            )
            self.renderer.running_queued_follow_up(len(self.state.queued_prompts))
            return await self._start_prompt(queued_prompt)
        self.state.cancel_requested = False
        self.state.status = TuiStatus.idle
        if event.ok:
            self._sync_view()
        elif was_cancelled:
            self._sync_view()
        else:
            self._update_view(
                status="error",
                input_hint=_prompt_for_mode(_InputMode.idle),
                input_mode=_InputMode.idle,
                queued_follow_ups=0,
            )
        return False

    def _handle_rpc_closed(self, signal: _RpcEventsClosed) -> bool:
        self._update_view(status="error")
        if signal.error is not None:
            self.renderer.rpc_event_reader_failed(signal.error)
        if self.state.token_stream_started:
            self.renderer.end_token_stream()
            self.state.token_stream_started = False
        if self.state.current_command_id is not None:
            self.renderer.rpc_stream_ended_before_command(self.state.current_command_id)
        elif self.state.shutdown_command_id is not None:
            self.renderer.rpc_stream_ended_before_shutdown(self.state.shutdown_command_id)
        elif signal.error is None:
            self.renderer.rpc_stream_ended_unexpectedly()
        return True

    def _render_help(self) -> None:
        self.renderer.help()

    def _render_event(self, event: KnownWispEvent) -> None:
        if self.state.cancel_requested:
            if isinstance(event, ErrorEvent) and _is_rpc_cancelled_message(event.message):
                return
            if (
                isinstance(event, RpcCommandFinished)
                and not event.ok
                and _is_rpc_cancelled_message(event.error)
            ):
                self.state.status = TuiStatus.idle
                self.state.cancel_requested = False
                self.state.queued_prompts.clear()
                self._sync_view()
                self.renderer.cancelled()
                return
        if isinstance(event, SessionSaved):
            self._update_view(last_session=_compact_session_path(event.path))
        if isinstance(event, ErrorEvent):
            self._update_view(status="error")
        if isinstance(event, RpcCommandFinished) and not event.ok:
            self._update_view(status="error")
        self.renderer.event(event)


async def _default_prompt_reader(prompt: str) -> str:
    selected_prompt = prompt if _stdin_is_interactive() else ""
    return await anyio.to_thread.run_sync(input, selected_prompt, abandon_on_cancel=True)


async def _preflight_tui_options(options: TuiOptions) -> None:
    runtime = await build_runtime()
    runtime.providers.get(options.config.provider)
    for tool_name in set(options.allowed_tools):
        runtime.tools.get(tool_name)
    sessions = JsonlSessionStore(options.config.session_dir)
    if options.resume is not None:
        sessions.load(options.resume)
    elif options.continue_latest:
        sessions.latest()


def _rpc_command(options: TuiOptions) -> tuple[str, ...]:
    command: list[str] = [
        sys.executable,
        "-m",
        "wisp",
        "--mode",
        "rpc",
        "--provider",
        options.config.provider,
        "--session-dir",
        str(options.config.session_dir),
    ]
    if options.config.model is not None:
        command.extend(("--model", options.config.model))
    if options.resume is not None:
        command.extend(("--resume", options.resume))
    if options.continue_latest:
        command.append("--continue")
    if options.allow_read_tools:
        command.append("--allow-read-tools")
    for tool_name in options.allowed_tools:
        command.extend(("--allow-tool", tool_name))
    if options.approve_unsafe_tools:
        command.append("--yes")
    if options.max_tool_iterations is not None:
        command.extend(("--max-tool-iterations", str(options.max_tool_iterations)))
    return tuple(command)


def _rpc_env() -> dict[str, str]:
    return dict(os.environ)


def _stdin_is_interactive() -> bool:
    isatty = getattr(sys.stdin, "isatty", None)
    return bool(isatty and isatty())


def _stdout_is_interactive() -> bool:
    isatty = getattr(sys.stdout, "isatty", None)
    return bool(isatty and isatty())


def _stdio_is_interactive() -> bool:
    return _stdin_is_interactive() and _stdout_is_interactive()


def _coerce_input_mode(value: str, *, fallback: _InputMode) -> _InputMode:
    try:
        return _InputMode(value)
    except ValueError:
        return fallback


def _input_mode_for_status(status: TuiStatus) -> _InputMode:
    if status is TuiStatus.waiting_for_approval:
        return _InputMode.approval
    if status is TuiStatus.running:
        return _InputMode.running
    if status is TuiStatus.exiting:
        return _InputMode.exiting
    return _InputMode.idle


def _view_status_for_status(status: TuiStatus) -> str:
    if status is TuiStatus.waiting_for_approval:
        return "waiting for approval"
    return status.value


def _prompt_for_mode(mode: _InputMode) -> str:
    if mode is _InputMode.approval:
        return "approve? [y/N] "
    if mode is _InputMode.running:
        return "wisp(running)> "
    if mode is _InputMode.exiting:
        return "wisp(exiting)> "
    return "wisp> "


def _compact_session_path(path: object) -> str:
    path_text = str(path)
    return os.path.basename(path_text) or path_text


def _is_rpc_cancelled_message(message: str | None) -> bool:
    return bool(message and message.startswith("RPC command cancelled:"))
