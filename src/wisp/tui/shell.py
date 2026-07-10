"""TUI shell and controller-facing event loop."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import anyio
from anyio.streams.memory import MemoryObjectSendStream
from rich.console import Console

from wisp.auth.openai_codex import OpenAICodexLoginMethod, login_openai_codex
from wisp.auth.storage import (
    ApiKeyCredential,
    AuthCredential,
    AuthStorageError,
    JsonAuthStore,
    OAuthCredential,
)
from wisp.config import DEFAULT_PROVIDER, default_auth_path
from wisp.events import (
    ErrorEvent,
    KnownWispEvent,
    MessageCompleted,
    MessageDelta,
    MessageStarted,
    ProjectConfigApplied,
    ProviderRetrying,
    RpcCommandFinished,
    SessionSaved,
    ToolApprovalRequested,
    TrustRequested,
)
from wisp.tui.commands import (
    TuiSlashCommand,
    TuiSlashCommandError,
    TuiSlashCommandName,
    parse_tui_slash_command,
)
from wisp.tui.launch import _stdin_is_interactive
from wisp.tui.live import LiveFullscreenInputInterrupted
from wisp.tui.rendering import TuiRenderer, TuiRendererKind, create_tui_renderer
from wisp.tui.state import (
    TuiInteractionState,
    TuiStatus,
    TuiViewState,
    _coerce_input_mode,
    _input_mode_for_status,
    _InputClosed,
    _InputInterrupted,
    _InputLine,
    _InputMode,
    _prompt_for_mode,
    _RpcEvent,
    _RpcEventsClosed,
    _TuiSignal,
    _view_status_for_status,
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

    async def trust(
        self,
        request_id: str,
        *,
        trusted: bool,
        reason: str | None = None,
        transient: bool = False,
        command_id: str | None = None,
    ) -> str: ...

    async def shutdown(self, *, command_id: str | None = None) -> str: ...

    async def configure(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        command_id: str | None = None,
    ) -> str: ...

    def events(self) -> AsyncIterator[KnownWispEvent]: ...

    async def close(self) -> None: ...


PromptReader = Callable[[str], Awaitable[str]]
_TRUST_ANSWERS = {"y", "yes", "n", "no"}


@dataclass(frozen=True)
class _PendingConfigure:
    command_id: str
    provider: str | None = None
    model: str | None = None
    reset_model: bool = False


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
        provider: str = DEFAULT_PROVIDER,
        model: str | None = None,
        auth_path: Path | None = None,
    ) -> None:
        self.controller = controller
        self.renderer = (
            renderer if renderer is not None else create_tui_renderer(TuiRendererKind.line, console)
        )
        self.prompt_reader = prompt_reader or _default_prompt_reader
        self.state = state or TuiInteractionState()
        self.view = TuiViewState(provider=provider, model=model)
        self.current_provider = provider
        self.current_model = model
        self.pending_configures: dict[str, _PendingConfigure] = {}
        self.auth_store = JsonAuthStore(auth_path or default_auth_path())

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
        self.view.provider = self.current_provider
        self.view.model = self.current_model
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
        try:
            command = parse_tui_slash_command(text)
        except TuiSlashCommandError as exc:
            self.renderer.command_error(str(exc))
            return False
        if command is not None:
            return await self._handle_slash_command(command)
        if self.state.pending_trust is not None:
            if signal.mode is _InputMode.trust or _is_trust_answer(text):
                return await self._answer_pending_trust(text)
            if text and self.state.current_command_id is not None:
                self.state.queued_prompts.append(text)
                self._update_view(queued_follow_ups=len(self.state.queued_prompts))
                self.renderer.queued_follow_up(len(self.state.queued_prompts))
            return False
        if self.state.pending_approval is not None:
            if signal.mode is _InputMode.approval:
                return await self._answer_pending_approval(text, exit_after_denial=False)
            if text and self.state.current_command_id is not None:
                self.state.queued_prompts.append(text)
                self._update_view(queued_follow_ups=len(self.state.queued_prompts))
                self.renderer.queued_follow_up(len(self.state.queued_prompts))
            return False
        if not text:
            return False
        if self.state.current_command_id is not None:
            self.state.queued_prompts.append(text)
            self._update_view(queued_follow_ups=len(self.state.queued_prompts))
            self.renderer.queued_follow_up(len(self.state.queued_prompts))
            return False
        return await self._start_prompt(text)

    async def _handle_slash_command(self, command: TuiSlashCommand) -> bool:
        if command.name is TuiSlashCommandName.help:
            self._render_help()
            return False
        if command.name is TuiSlashCommandName.quit:
            return await self._handle_quit()
        if self.state.pending_approval is not None:
            self.renderer.command_error("Cannot run slash commands while approval is pending.")
            return False
        if self.state.current_command_id is not None:
            self.renderer.command_error("Cannot run slash commands while a prompt is running.")
            return False
        if command.name is TuiSlashCommandName.auth:
            self._handle_auth_status_command(command.args)
            return False
        if command.name is TuiSlashCommandName.login:
            await self._handle_login_command(command.args)
            return False
        if command.name is TuiSlashCommandName.logout:
            self._handle_logout_command(command.args)
            return False
        if command.name is TuiSlashCommandName.provider:
            await self._handle_provider_command(command.args)
            return False
        if command.name is TuiSlashCommandName.model:
            await self._handle_model_command(command.args)
            return False
        self.renderer.command_error(f"Unknown command: /{command.name.value}")
        return False

    def _handle_auth_status_command(self, args: tuple[str, ...]) -> None:
        if len(args) > 1:
            self.renderer.command_error("Usage: /auth [provider]")
            return
        provider = args[0] if args else self._default_auth_provider()
        try:
            credential = self.auth_store.get(provider)
        except AuthStorageError as exc:
            self.renderer.command_error(f"Auth storage error: {exc}")
            return
        self.renderer.notice(_auth_status_line(provider, credential))

    async def _handle_login_command(self, args: tuple[str, ...]) -> None:
        if len(args) > 2:
            self.renderer.command_error("Usage: /login [provider] [device-code]")
            return
        provider = args[0] if args else self._default_auth_provider()
        if provider != "openai-codex":
            self.renderer.command_error("TUI login currently supports only openai-codex.")
            return
        method_text = args[1] if len(args) == 2 else OpenAICodexLoginMethod.device_code.value
        try:
            method = OpenAICodexLoginMethod(method_text)
        except ValueError:
            self.renderer.command_error("Usage: /login [openai-codex] [device-code]")
            return
        if method is OpenAICodexLoginMethod.browser:
            self.renderer.command_error(
                "Browser login is not available inside the TUI; use `wisp auth login openai-codex`."
            )
            return
        self.renderer.notice("Starting openai-codex device-code login...")
        try:
            credential = await login_openai_codex(
                method=method,
                on_device_code=lambda info: self.renderer.notice(
                    f"Open {info.verification_uri} and enter code {info.user_code}"
                ),
                open_browser=False,
            )
        except Exception as exc:  # noqa: BLE001 - show login failure in the TUI
            self.renderer.command_error(f"Login failed: {exc}")
            return
        try:
            self.auth_store.set(provider, credential)
        except AuthStorageError as exc:
            self.renderer.command_error(f"Auth storage error: {exc}")
            return
        self.renderer.notice(f"Logged in: {provider}")

    def _handle_logout_command(self, args: tuple[str, ...]) -> None:
        if len(args) > 1:
            self.renderer.command_error("Usage: /logout [provider]")
            return
        provider = args[0] if args else self._default_auth_provider()
        try:
            deleted = self.auth_store.delete(provider)
        except AuthStorageError as exc:
            self.renderer.command_error(f"Auth storage error: {exc}")
            return
        if deleted:
            self.renderer.notice(f"Logged out: {provider}")
        else:
            self.renderer.notice(f"Not logged in: {provider}")

    async def _handle_provider_command(self, args: tuple[str, ...]) -> None:
        if len(args) > 1:
            self.renderer.command_error("Usage: /provider [provider]")
            return
        if not args:
            line = f"Current provider: {self.current_provider}"
            pending_provider = self._latest_pending_provider()
            if pending_provider is not None:
                line += f" (pending: {pending_provider})"
            self.renderer.notice(line)
            return
        provider = args[0]
        try:
            command_id = await self.controller.configure(provider=provider)
        except Exception as exc:  # noqa: BLE001 - show send failure in the TUI
            self.renderer.send_failed("configure", exc)
            return
        self.pending_configures[command_id] = _PendingConfigure(
            command_id=command_id,
            provider=provider,
            reset_model=True,
        )
        self._update_view(status="configuring")
        self.renderer.notice(f"Configuring provider: {provider}")

    async def _handle_model_command(self, args: tuple[str, ...]) -> None:
        if len(args) > 1:
            self.renderer.command_error("Usage: /model [model]")
            return
        if not args:
            line = f"Current model: {self.current_model or 'provider default'}"
            pending_model = self._latest_pending_model()
            if pending_model is not None:
                line += f" (pending: {pending_model})"
            self.renderer.notice(line)
            return
        model = args[0]
        try:
            command_id = await self.controller.configure(model=model)
        except Exception as exc:  # noqa: BLE001 - show send failure in the TUI
            self.renderer.send_failed("configure", exc)
            return
        self.pending_configures[command_id] = _PendingConfigure(
            command_id=command_id,
            model=model,
        )
        self._update_view(status="configuring")
        self.renderer.notice(f"Configuring model: {model}")

    async def _handle_input_closed(self, signal: _InputClosed) -> bool:
        self.state.input_closed = True
        if self.state.status is TuiStatus.exiting:
            return False
        self.state.exit_requested = True
        if self.state.pending_trust is not None:
            # Resolve pending trust as untrusted (safe) so the RPC side unblocks.
            return await self._answer_pending_trust(
                "",
                trusted=False,
                reason="Trust prompt closed",
                transient=True,
            )
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
        if self.state.pending_trust is not None:
            return await self._answer_pending_trust(
                "",
                trusted=False,
                reason="Trust prompt interrupted",
                transient=True,
            )
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
        if self.state.pending_trust is not None:
            return await self._answer_pending_trust(
                "",
                trusted=False,
                reason="Trust prompt: quit requested",
                transient=True,
            )
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

    async def _answer_pending_trust(
        self,
        answer: str,
        *,
        trusted: bool | None = None,
        reason: str | None = None,
        transient: bool = False,
    ) -> bool:
        trust = self.state.pending_trust
        if trust is None:
            return False
        selected_trusted = (
            trusted if trusted is not None else answer.strip().lower() in {"y", "yes"}
        )
        selected_reason = reason if not selected_trusted else None
        ok = await self._send_trust(
            trust.request_id,
            trusted=selected_trusted,
            reason=selected_reason,
            transient=transient and not selected_trusted,
        )
        self.state.pending_trust = None
        if not ok:
            return True
        self.state.status = TuiStatus.running if self.state.current_command_id else TuiStatus.idle
        self._sync_view()
        return False

    async def _send_trust(
        self,
        request_id: str,
        *,
        trusted: bool,
        reason: str | None,
        transient: bool,
    ) -> bool:
        try:
            await self.controller.trust(
                request_id,
                trusted=trusted,
                reason=reason,
                transient=transient,
            )
        except Exception as exc:
            self._update_view(status="error")
            self.renderer.send_failed("trust", exc)
            return False
        return True

    async def _handle_rpc_event(self, event: KnownWispEvent) -> bool:
        if isinstance(event, ProviderRetrying):
            self._update_view(
                status=(
                    f"retrying {event.attempt}/{event.max_attempts} in {event.delay_seconds:.1f}s"
                ),
                input_hint=_prompt_for_mode(_InputMode.running),
                input_mode=_InputMode.running,
                queued_follow_ups=len(self.state.queued_prompts),
            )
            self.renderer.event(event)
            return False
        if isinstance(event, MessageStarted):
            self._sync_view()
        if isinstance(event, MessageDelta) and event.content_kind == "text":
            self.state.token_stream_started = True
            self.state.rendered_tokens = True
            self.renderer.token_delta(event.delta)
            return False
        if self.state.token_stream_started:
            self.renderer.end_token_stream()
            self.state.token_stream_started = False
        if isinstance(event, MessageCompleted):
            suppress_completed_message = self.state.rendered_tokens
            self.state.rendered_tokens = False
            if suppress_completed_message:
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

        if isinstance(event, TrustRequested):
            self.state.pending_trust = event
            self.state.status = TuiStatus.waiting_for_trust
            self._sync_view()
            self.renderer.trust_request(event)
            if self.state.input_closed:
                # No way to answer: default to untrusted (safe) so the run proceeds.
                # Mark it transient so the gate does not persist a denial the user
                # never explicitly chose.
                return await self._answer_pending_trust(
                    "",
                    trusted=False,
                    reason="Trust prompt: input closed",
                    transient=True,
                )
            return False

        if isinstance(event, ProjectConfigApplied):
            # The RPC side applied a trusted project's config mid-session (first-run
            # approval). Adopt the provider/model/auth it now runs with, so the header
            # and /provider,/model,/auth,/login stop showing the untrusted-startup ones.
            self.current_provider = event.provider
            self.current_model = event.model
            self.auth_store = JsonAuthStore(event.auth_path)
            self.renderer.notice(
                f"Applied trusted project config: provider {event.provider}"
                f"{f', model {event.model}' if event.model else ''}."
            )
            self._sync_view()
            return False

        if isinstance(event, RpcCommandFinished):
            if event.command_id in self.pending_configures:
                self._finish_pending_configure(event)
                return False
            if event.command_id == self.state.shutdown_command_id:
                self._render_event(event)
                return True
            if event.command_id == self.state.current_command_id:
                self._render_event(event)
                return await self._finish_current_prompt(event)
        self._render_event(event)
        return False

    def _finish_pending_configure(self, event: RpcCommandFinished) -> None:
        pending = self.pending_configures.pop(event.command_id)
        if event.ok:
            if pending.provider is not None:
                self.current_provider = pending.provider
                if pending.reset_model:
                    self.current_model = None
                self.renderer.notice(
                    f"Provider set to {pending.provider}; model reset to provider default."
                )
            if pending.model is not None:
                self.current_model = pending.model
                self.renderer.notice(f"Model set to {pending.model}")
            self._sync_view()
            return
        message = event.error or "configure failed"
        if pending.provider is not None:
            self.renderer.command_error(f"Provider unchanged ({self.current_provider}): {message}")
        elif pending.model is not None:
            self.renderer.command_error(
                f"Model unchanged ({self.current_model or 'provider default'}): {message}"
            )
        self._update_view(
            status="error",
            input_hint=_prompt_for_mode(_InputMode.idle),
            input_mode=_InputMode.idle,
            queued_follow_ups=len(self.state.queued_prompts),
        )

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
        elif self.pending_configures:
            command_id = next(iter(self.pending_configures))
            self.renderer.rpc_stream_ended_before_command(command_id)
        elif self.state.shutdown_command_id is not None:
            self.renderer.rpc_stream_ended_before_shutdown(self.state.shutdown_command_id)
        elif signal.error is None:
            self.renderer.rpc_stream_ended_unexpectedly()
        return True

    def _default_auth_provider(self) -> str:
        return self._latest_pending_provider() or self.current_provider

    def _latest_pending_provider(self) -> str | None:
        for pending in reversed(self.pending_configures.values()):
            if pending.provider is not None:
                return pending.provider
        return None

    def _latest_pending_model(self) -> str | None:
        for pending in reversed(self.pending_configures.values()):
            if pending.model is not None:
                return pending.model
        return None

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


def _auth_status_line(provider: str, credential: AuthCredential | None) -> str:
    if credential is None:
        return f"{provider}: not logged in"
    if isinstance(credential, ApiKeyCredential):
        return f"{provider}: api key configured"
    return f"{provider}: oauth configured ({_oauth_expiry_text(credential)})"


def _oauth_expiry_text(credential: OAuthCredential) -> str:
    expires = datetime.fromtimestamp(credential.expires / 1000, tz=UTC)
    return f"expires {expires.isoformat()}"


def _compact_session_path(path: object) -> str:
    path_text = str(path)
    return os.path.basename(path_text) or path_text


def _is_rpc_cancelled_message(message: str | None) -> bool:
    return bool(message and message.startswith("RPC command cancelled:"))


def _is_trust_answer(text: str) -> bool:
    return text.strip().lower() in _TRUST_ANSWERS
