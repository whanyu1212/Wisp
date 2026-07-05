"""Command-line interface for Wisp."""

from __future__ import annotations

import json
import os
import stat
import sys
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Annotated, cast
from uuid import uuid4

import anyio
import typer
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream
from rich.console import Console

from wisp.agent.loop import Agent
from wisp.agent.messages import Message
from wisp.cli.auth import auth_app
from wisp.config import WispConfig, load_project_env
from wisp.events import ErrorEvent, RpcCommandFinished, RpcCommandStarted, TokenDelta
from wisp.providers.base import ProviderError
from wisp.runtime.api import WispRuntime
from wisp.runtime.extensions import build_runtime
from wisp.runtime.registry import UnknownProviderError, UnknownToolError
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore, SessionError
from wisp.tools.approval import ToolApprovalDecision, ToolApprovalPolicy
from wisp.tools.base import Tool
from wisp.tui.rendering import TuiRendererKind

from . import options as _cli_options
from . import output as _cli_output
from . import tools as _cli_tools
from .types import OutputMode, _JsonOutputModeError


@dataclass(frozen=True)
class _RpcInputCommand:
    command: dict[str, object]


@dataclass(frozen=True)
class _RpcInputClosed:
    pass


@dataclass(frozen=True)
class _RpcPromptCompleted:
    command_id: str
    ok: bool
    history: tuple[Message, ...] | None
    entry_count: int


@dataclass
class _RpcSessionState:
    session: JsonlSession | None
    history: tuple[Message, ...]
    entry_count: int


@dataclass(frozen=True)
class _RpcRunningPrompt:
    command_id: str
    cancel_scope: anyio.CancelScope


@dataclass
class _RpcPendingApproval:
    call_id: str
    event: anyio.Event
    approved: bool | None = None
    reason: str | None = None
    resolved: bool = False


type _RpcControlEvent = _RpcInputCommand | _RpcInputClosed | _RpcPromptCompleted


_STDIN_READ_CHUNK_SIZE = 64 * 1024
_STDIN_THREAD_POLL_INTERVAL = 0.01
_STDIN_THREAD_QUEUE_SIZE = 100
_MAX_QUEUED_RPC_COMMANDS = 100

# Compatibility aliases for callers/tests that import private helpers from wisp.cli.
_env_value = _cli_options._env_value
_has_callback_cli_args = _cli_options._has_callback_cli_args
_option_was_provided = _cli_options._option_was_provided
_output_mode_from_env = _cli_options._output_mode_from_env
_resolve_cli_mode = _cli_options._resolve_cli_mode
_resolve_tui_renderer = _cli_options._resolve_tui_renderer
_tui_renderer_from_env = _cli_options._tui_renderer_from_env

_exit_with_error = _cli_output._exit_with_error
_format_event_arguments = _cli_output._format_event_arguments
_format_event_output = _cli_output._format_event_output
_print_event_line = _cli_output._print_event_line
_render_json_events = _cli_output._render_json_events
_render_print_event = _cli_output._render_print_event
_truncate_inline = _cli_output._truncate_inline
_write_json_event = _cli_output._write_json_event
_writes_json_events = _cli_output._writes_json_events

_print_mode_tool_approval_policy = _cli_tools._print_mode_tool_approval_policy
_print_mode_tool_registry = _cli_tools._print_mode_tool_registry
_session_for_print_run = _cli_tools._session_for_print_run


class _RpcToolApprovalPolicy(ToolApprovalPolicy):
    """Tool approval policy that can wait for RPC approval responses."""

    def __init__(self, fallback: ToolApprovalPolicy) -> None:
        super().__init__(
            approved_tools=fallback.approved_tools,
            approved_safety=fallback.approved_safety,
        )
        self._pending: dict[str, _RpcPendingApproval] = {}
        self._input_closed_reason: str | None = None

    def prepare_approval(
        self,
        tool: Tool,
        *,
        call_id: str,
        arguments: Mapping[str, object],
    ) -> None:
        if self.approves(tool):
            return
        pending = _RpcPendingApproval(call_id=call_id, event=anyio.Event())
        self._pending[call_id] = pending
        if self._input_closed_reason is not None:
            self._resolve_pending(
                pending,
                approved=False,
                reason=self._input_closed_reason,
            )

    async def await_approval(
        self,
        tool: Tool,
        *,
        call_id: str,
        arguments: Mapping[str, object],
    ) -> ToolApprovalDecision:
        if self.approves(tool):
            return ToolApprovalDecision(approved=True)
        pending = self._pending.get(call_id)
        if pending is None:
            return ToolApprovalDecision(approved=False, reason=self.block_reason(tool))
        try:
            await pending.event.wait()
            approved = pending.approved is True
            reason = None if approved else pending.reason or "Tool execution was denied"
            return ToolApprovalDecision(approved=approved, reason=reason)
        finally:
            self._pending.pop(call_id, None)

    def resolve_approval(
        self,
        *,
        call_id: str,
        approved: bool,
        reason: str | None = None,
    ) -> bool:
        pending = self._pending.get(call_id)
        if pending is None or pending.resolved:
            return False
        self._resolve_pending(pending, approved=approved, reason=reason)
        return True

    def deny_pending_on_input_closed(self) -> None:
        reason = "RPC input closed before approval response"
        self._input_closed_reason = reason
        for pending in tuple(self._pending.values()):
            if not pending.resolved:
                self._resolve_pending(pending, approved=False, reason=reason)

    def _resolve_pending(
        self,
        pending: _RpcPendingApproval,
        *,
        approved: bool,
        reason: str | None,
    ) -> None:
        pending.resolved = True
        pending.approved = approved
        pending.reason = None if approved else reason
        pending.event.set()


app = typer.Typer(
    add_completion=False,
    help="Wisp: a Python, Pi-inspired coding agent.",
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(auth_app, name="auth")


@app.callback()
def cli_callback(
    ctx: typer.Context,
    prompt: Annotated[
        str | None,
        typer.Option("--prompt", "-p", help="Run one prompt and exit."),
    ] = None,
    provider: Annotated[
        str | None,
        typer.Option(help="Provider to use, e.g. fake or openai."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(help="Model name for the selected provider."),
    ] = None,
    session_dir: Annotated[
        Path | None,
        typer.Option(help="Directory for JSONL session files."),
    ] = None,
    auth_file: Annotated[
        Path | None,
        typer.Option(help="Path to Wisp's private provider auth JSON file."),
    ] = None,
    mode: Annotated[
        OutputMode,
        typer.Option("--mode", help="Output mode: text, JSONL events, RPC, or TUI."),
    ] = OutputMode.text,
    tui_renderer: Annotated[
        TuiRendererKind,
        typer.Option(
            "--tui-renderer",
            help="TUI renderer to use with --mode tui.",
        ),
    ] = TuiRendererKind.line,
    allow_read_tools: Annotated[
        bool,
        typer.Option(help="Expose sandboxed read-only tools in agent modes."),
    ] = False,
    allow_tool: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-tool",
            help="Expose a specific tool in agent modes. Can be repeated.",
        ),
    ] = None,
    resume: Annotated[
        str | None,
        typer.Option(
            "--resume", help="Continue a session by JSONL path, filename, id, or id prefix."
        ),
    ] = None,
    continue_latest: Annotated[
        bool,
        typer.Option("--continue", help="Continue the latest session in the session directory."),
    ] = False,
    approve_unsafe_tools: Annotated[
        bool,
        typer.Option(
            "--yes",
            "--allow-unsafe-tool-execution",
            help="Approve non-interactive execution of mutating and command tools.",
        ),
    ] = False,
    max_tool_iterations: Annotated[
        int | None,
        typer.Option(
            "--max-tool-iterations",
            help="Optional cap on model/tool rounds. Defaults to uncapped.",
        ),
    ] = None,
) -> None:
    """Run Wisp in the initial print-mode CLI."""

    if ctx.invoked_subcommand is not None:
        return

    console = Console(stderr=True)
    load_project_env()
    mode_was_provided = _option_was_provided(ctx, "mode")
    resolved_mode = _resolve_cli_mode(
        mode,
        prompt=prompt,
        mode_was_provided=mode_was_provided,
        console=console,
    )
    resolved_tui_renderer = tui_renderer
    if resolved_mode is OutputMode.tui:
        resolved_tui_renderer = _resolve_tui_renderer(
            tui_renderer,
            renderer_was_provided=_option_was_provided(ctx, "tui_renderer"),
            console=console,
        )

    if prompt is None and resolved_mode is not OutputMode.tui and not _has_callback_cli_args(ctx):
        typer.echo(ctx.get_help())
        raise typer.Exit(0)

    if resolved_mode is OutputMode.rpc:
        if prompt is not None:
            _exit_with_error(
                "--prompt is not used with --mode rpc; send prompt commands on stdin",
                mode=resolved_mode,
                console=console,
            )
    elif resolved_mode is OutputMode.tui:
        if prompt is not None:
            _exit_with_error(
                "--prompt is not used with --mode tui; enter prompts in the TUI",
                mode=resolved_mode,
                console=console,
            )
    elif prompt is None:
        # Keep direct calls to the callback friendly in tests or embedded usage.
        raise typer.Exit(0)

    if resume is not None and continue_latest:
        _exit_with_error(
            "use either --resume or --continue, not both",
            mode=resolved_mode,
            console=console,
        )
    if max_tool_iterations is not None and max_tool_iterations < 0:
        _exit_with_error(
            "--max-tool-iterations must be non-negative",
            mode=resolved_mode,
            console=console,
        )

    config = WispConfig.from_env(
        provider=provider,
        model=model,
        session_dir=session_dir,
        auth_path=auth_file,
        load_env_file=False,
    )
    try:
        if resolved_mode is OutputMode.rpc:
            anyio.run(
                _run_rpc,
                config,
                allow_read_tools,
                tuple(allow_tool or ()),
                resume,
                continue_latest,
                approve_unsafe_tools,
                max_tool_iterations,
            )
        elif resolved_mode is OutputMode.tui:
            from wisp.tui import TuiOptions, run_tui

            anyio.run(
                run_tui,
                TuiOptions(
                    config=config,
                    allow_read_tools=allow_read_tools,
                    allowed_tools=tuple(allow_tool or ()),
                    resume=resume,
                    continue_latest=continue_latest,
                    approve_unsafe_tools=approve_unsafe_tools,
                    max_tool_iterations=max_tool_iterations,
                    renderer=resolved_tui_renderer,
                ),
            )
        else:
            assert prompt is not None
            anyio.run(
                _run_print,
                prompt,
                config,
                allow_read_tools,
                tuple(allow_tool or ()),
                resume,
                continue_latest,
                approve_unsafe_tools,
                max_tool_iterations,
                resolved_mode,
            )
    except _JsonOutputModeError as exc:
        raise typer.Exit(1) from exc
    except (ProviderError, SessionError, UnknownProviderError, UnknownToolError) as exc:
        if _writes_json_events(resolved_mode):
            _write_json_event(ErrorEvent(message=str(exc)))
        else:
            console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1) from exc


def main() -> None:
    """Console-script entry point."""

    app()


async def _build_runtime_for_config(config: WispConfig) -> WispRuntime:
    try:
        return await build_runtime(auth_path=config.auth_path)
    except TypeError as exc:
        if "auth_path" not in str(exc):
            raise
        return await build_runtime()


async def _run_print(
    prompt: str,
    config: WispConfig,
    allow_read_tools: bool = False,
    allowed_tools: tuple[str, ...] = (),
    resume: str | None = None,
    continue_latest: bool = False,
    approve_unsafe_tools: bool = False,
    max_tool_iterations: int | None = None,
    mode: OutputMode = OutputMode.text,
) -> None:
    runtime = await _build_runtime_for_config(config)
    provider = runtime.providers.get(config.provider)
    sessions = JsonlSessionStore(config.session_dir)
    session = _session_for_print_run(sessions, resume=resume, continue_latest=continue_latest)
    history = session.read_messages() if session is not None else ()
    agent = Agent(
        provider=provider,
        sessions=sessions,
        events=runtime.events,
        model=config.model,
        tool_registry=_print_mode_tool_registry(
            runtime.tools,
            allow_read_tools=allow_read_tools,
            allowed_tools=allowed_tools,
        ),
        tool_approval_policy=_print_mode_tool_approval_policy(approve_unsafe_tools),
        max_tool_iterations=max_tool_iterations,
    )

    events = agent.run(prompt, session=session, history=history)
    if mode is OutputMode.json:
        await _render_json_events(events)
        return

    event_console = Console(stderr=True, soft_wrap=True)
    wrote_tokens = False
    stderr_needs_separator = False
    async for event in events:
        if isinstance(event, TokenDelta):
            sys.stdout.write(event.delta)
            sys.stdout.flush()
            wrote_tokens = True
            stderr_needs_separator = True
        elif isinstance(event, ErrorEvent):
            raise ProviderError(event.message)
        else:
            if stderr_needs_separator and _print_event_line(event) is not None:
                event_console.print()
                stderr_needs_separator = False
            _render_print_event(event, event_console)

    if wrote_tokens:
        sys.stdout.write("\n")


async def _run_rpc(
    config: WispConfig,
    allow_read_tools: bool = False,
    allowed_tools: tuple[str, ...] = (),
    resume: str | None = None,
    continue_latest: bool = False,
    approve_unsafe_tools: bool = False,
    max_tool_iterations: int | None = None,
) -> None:
    runtime = await _build_runtime_for_config(config)
    provider = runtime.providers.get(config.provider)
    sessions = JsonlSessionStore(config.session_dir)
    session = _session_for_print_run(sessions, resume=resume, continue_latest=continue_latest)
    session_state = _rpc_session_state(session)
    approval_policy = _RpcToolApprovalPolicy(_print_mode_tool_approval_policy(approve_unsafe_tools))
    agent = Agent(
        provider=provider,
        sessions=sessions,
        events=runtime.events,
        model=config.model,
        tool_registry=_print_mode_tool_registry(
            runtime.tools,
            allow_read_tools=allow_read_tools,
            allowed_tools=allowed_tools,
        ),
        tool_approval_policy=approval_policy,
        max_tool_iterations=max_tool_iterations,
    )

    queued_commands: deque[dict[str, object]] = deque()
    running_prompt: _RpcRunningPrompt | None = None
    stdin_closed = False
    send, receive = anyio.create_memory_object_stream[_RpcControlEvent](100)
    stop_reader = anyio.Event()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(_read_rpc_stdin, send.clone(), stop_reader)
        async with send, receive:
            while True:
                if running_prompt is None and queued_commands:
                    command = queued_commands.popleft()
                    running_prompt, should_shutdown = _dispatch_rpc_command(
                        command,
                        agent=agent,
                        runtime=runtime,
                        sessions=sessions,
                        session_state=session_state,
                        task_group=task_group,
                        send=send,
                        running_prompt=running_prompt,
                        approval_policy=approval_policy,
                    )
                    if should_shutdown:
                        stop_reader.set()
                        task_group.cancel_scope.cancel()
                        return
                    continue
                if stdin_closed and running_prompt is None and not queued_commands:
                    stop_reader.set()
                    task_group.cancel_scope.cancel()
                    return

                control_event = await receive.receive()
                if isinstance(control_event, _RpcInputClosed):
                    stdin_closed = True
                    approval_policy.deny_pending_on_input_closed()
                    continue
                if isinstance(control_event, _RpcPromptCompleted):
                    if (
                        running_prompt is not None
                        and control_event.command_id == running_prompt.command_id
                    ):
                        running_prompt = None
                        session_state.entry_count = control_event.entry_count
                        if control_event.history is not None:
                            session_state.history = control_event.history
                    continue

                command = control_event.command
                command_type = _rpc_command_type(command)
                if running_prompt is not None and command_type not in {"approval", "cancel"}:
                    if len(queued_commands) >= _MAX_QUEUED_RPC_COMMANDS:
                        _reject_rpc_command(
                            command,
                            message="RPC command queue is full while a prompt is running",
                        )
                        continue
                    queued_commands.append(command)
                    continue
                running_prompt, should_shutdown = _dispatch_rpc_command(
                    command,
                    agent=agent,
                    runtime=runtime,
                    sessions=sessions,
                    session_state=session_state,
                    task_group=task_group,
                    send=send,
                    running_prompt=running_prompt,
                    approval_policy=approval_policy,
                )
                if should_shutdown:
                    stop_reader.set()
                    task_group.cancel_scope.cancel()
                    return


def _dispatch_rpc_command(
    command: dict[str, object],
    *,
    agent: Agent,
    runtime: WispRuntime,
    sessions: JsonlSessionStore,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    running_prompt: _RpcRunningPrompt | None,
    approval_policy: _RpcToolApprovalPolicy,
) -> tuple[_RpcRunningPrompt | None, bool]:
    command_type = _rpc_command_type(command)
    if command_type == "prompt":
        new_running_prompt, new_session = _start_rpc_prompt_command(
            command,
            agent=agent,
            sessions=sessions,
            session_state=session_state,
            task_group=task_group,
            send=send,
        )
        if new_session is not None:
            session_state.session = new_session
        return new_running_prompt, False
    should_shutdown = _handle_rpc_control_command(
        command,
        agent=agent,
        runtime=runtime,
        running_prompt=running_prompt,
        approval_policy=approval_policy,
    )
    return running_prompt, should_shutdown


def _rpc_session_state(session: JsonlSession | None) -> _RpcSessionState:
    if session is None or not session.path.is_file():
        return _RpcSessionState(session=session, history=(), entry_count=0)
    return _RpcSessionState(
        session=session,
        history=session.read_messages(),
        entry_count=len(session.read_entries()),
    )


async def _read_rpc_stdin(
    send: MemoryObjectSendStream[_RpcControlEvent],
    stop_reader: anyio.Event,
) -> None:
    async with send:
        try:
            fd = sys.stdin.fileno()
            stdin_mode = os.fstat(fd).st_mode
        except (AttributeError, OSError, ValueError):
            await _read_rpc_text_stdin(send, stop_reader)
            return
        if stat.S_ISREG(stdin_mode):
            await _read_rpc_text_stdin(send, stop_reader)
            return
        if _rpc_stdin_needs_thread_reader(stdin_mode):
            await _read_rpc_thread_stdin(send, stop_reader)
            return
        await _read_rpc_fd_stdin(send, stop_reader, fd)


def _rpc_stdin_needs_thread_reader(stdin_mode: int) -> bool:
    return os.name != "posix" and not stat.S_ISREG(stdin_mode)


async def _read_rpc_text_stdin(
    send: MemoryObjectSendStream[_RpcControlEvent],
    stop_reader: anyio.Event,
) -> None:
    while not stop_reader.is_set():
        raw_line = await anyio.to_thread.run_sync(sys.stdin.readline)
        if raw_line == "":
            await send.send(_RpcInputClosed())
            return
        await _send_rpc_input_line(send, raw_line)


async def _read_rpc_thread_stdin(
    send: MemoryObjectSendStream[_RpcControlEvent],
    stop_reader: anyio.Event,
) -> None:
    lines: Queue[str | Exception] = Queue(maxsize=_STDIN_THREAD_QUEUE_SIZE)
    stdin = sys.stdin

    def read_lines() -> None:
        try:
            while True:
                raw_line = stdin.readline()
                lines.put(raw_line)
                if raw_line == "":
                    return
        except Exception as exc:  # noqa: BLE001 - surface stdin reader failures as RPC errors
            lines.put(exc)

    Thread(target=read_lines, name="wisp-rpc-stdin-reader", daemon=True).start()
    while not stop_reader.is_set():
        try:
            item = lines.get_nowait()
        except Empty:
            await anyio.sleep(_STDIN_THREAD_POLL_INTERVAL)
            continue
        if isinstance(item, Exception):
            _write_json_event(ErrorEvent(message=f"Failed to read RPC stdin: {item}"))
            await send.send(_RpcInputClosed())
            return
        if item == "":
            await send.send(_RpcInputClosed())
            return
        await _send_rpc_input_line(send, item)


async def _read_rpc_fd_stdin(
    send: MemoryObjectSendStream[_RpcControlEvent],
    stop_reader: anyio.Event,
    fd: int,
) -> None:
    buffer = bytearray()
    while not stop_reader.is_set():
        await anyio.wait_readable(fd)
        if stop_reader.is_set():
            return
        try:
            chunk = os.read(fd, _STDIN_READ_CHUNK_SIZE)
        except BlockingIOError:
            continue
        if chunk == b"":
            if buffer:
                await _send_rpc_input_line(send, _decode_rpc_stdin_line(buffer))
            await send.send(_RpcInputClosed())
            return
        buffer.extend(chunk)
        while True:
            newline_index = buffer.find(b"\n")
            if newline_index < 0:
                break
            line = _decode_rpc_stdin_line(buffer[:newline_index])
            del buffer[: newline_index + 1]
            await _send_rpc_input_line(send, line)


async def _send_rpc_input_line(
    send: MemoryObjectSendStream[_RpcControlEvent],
    raw_line: str,
) -> None:
    line = raw_line.strip()
    if not line:
        return
    command = _parse_rpc_command(line)
    if command is not None:
        await send.send(_RpcInputCommand(command=command))


def _decode_rpc_stdin_line(raw_line: bytes | bytearray) -> str:
    return bytes(raw_line).decode("utf-8", errors="replace")


def _start_rpc_prompt_command(
    command: dict[str, object],
    *,
    agent: Agent,
    sessions: JsonlSessionStore,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
) -> tuple[_RpcRunningPrompt | None, JsonlSession | None]:
    command_type, command_id, id_error = _rpc_command_identity(command)
    _write_json_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
        )
        return None, session_state.session

    prompt = command.get("prompt")
    if not isinstance(prompt, str):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC prompt command requires string field: prompt",
        )
        return None, session_state.session

    selected_session = session_state.session or sessions.create()
    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        _run_rpc_prompt_command,
        agent,
        selected_session,
        session_state.history,
        session_state.entry_count,
        prompt,
        command_id,
        command_type,
        cancel_scope,
        send.clone(),
    )
    return _RpcRunningPrompt(command_id=command_id, cancel_scope=cancel_scope), selected_session


async def _run_rpc_prompt_command(
    agent: Agent,
    session: JsonlSession,
    committed_history: tuple[Message, ...],
    entry_start: int,
    prompt: str,
    command_id: str,
    command_type: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
) -> None:
    error: str | None = None
    try:
        with cancel_scope:
            try:
                await _render_json_events(
                    agent.run(prompt, session=session, history=committed_history)
                )
            except _JsonOutputModeError as exc:
                error = str(exc)
            except anyio.get_cancelled_exc_class():
                error = f"RPC command cancelled: {command_id}"
    finally:
        cancelled = error is not None and error.startswith("RPC command cancelled:")
        if cancelled:
            await session.truncate_entries(entry_start)
        entry_count = (
            entry_start
            if cancelled
            else len(session.read_entries())
            if session.path.is_file()
            else entry_start
        )
        updated_history = (
            None if cancelled else _updated_rpc_history(session, committed_history, entry_start)
        )
        async with send:
            if cancelled:
                assert error is not None
                _write_json_event(ErrorEvent(message=error))
            _write_json_event(
                RpcCommandFinished(
                    command_id=command_id,
                    command_type=command_type,
                    ok=error is None,
                    error=error,
                )
            )
            await send.send(
                _RpcPromptCompleted(
                    command_id=command_id,
                    ok=error is None,
                    history=updated_history,
                    entry_count=entry_count,
                )
            )


def _updated_rpc_history(
    session: JsonlSession,
    committed_history: tuple[Message, ...],
    entry_start: int,
) -> tuple[Message, ...]:
    if not session.path.is_file():
        return committed_history
    entries = session.read_entries()
    new_messages = tuple(
        entry.message
        for entry in entries[entry_start:]
        if entry.kind == "message" and entry.message is not None
    )
    return (*committed_history, *new_messages)


def _reject_rpc_command(command: dict[str, object], *, message: str) -> None:
    command_type, command_id, id_error = _rpc_command_identity(command)
    _write_json_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    _write_rpc_command_error(
        command_id=command_id,
        command_type=command_type,
        message=id_error or message,
    )


def _handle_rpc_control_command(
    command: dict[str, object],
    *,
    running_prompt: _RpcRunningPrompt | None,
    approval_policy: _RpcToolApprovalPolicy,
    agent: Agent | None = None,
    runtime: WispRuntime | None = None,
) -> bool:
    command_type, command_id, id_error = _rpc_command_identity(command)
    _write_json_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
        )
        return False
    if command_type == "shutdown":
        _write_json_event(
            RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True)
        )
        return True
    if command_type == "cancel":
        _handle_rpc_cancel_command(
            command,
            command_id=command_id,
            command_type=command_type,
            running_prompt=running_prompt,
        )
        return False
    if command_type == "approval":
        _handle_rpc_approval_command(
            command,
            command_id=command_id,
            command_type=command_type,
            approval_policy=approval_policy,
        )
        return False
    if command_type == "configure":
        if agent is None or runtime is None:
            _write_rpc_command_error(
                command_id=command_id,
                command_type=command_type,
                message="RPC configure command requires an active agent runtime",
            )
            return False
        _handle_rpc_configure_command(
            command,
            command_id=command_id,
            command_type=command_type,
            agent=agent,
            runtime=runtime,
        )
        return False
    message = f"Unknown RPC command: {command_type}"
    _write_rpc_command_error(command_id=command_id, command_type=command_type, message=message)
    return False


def _handle_rpc_configure_command(
    command: dict[str, object],
    *,
    command_id: str,
    command_type: str,
    agent: Agent,
    runtime: WispRuntime,
) -> None:
    provider = command.get("provider")
    model = command.get("model")
    has_provider = "provider" in command
    has_model = "model" in command
    if not has_provider and not has_model:
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC configure command requires provider or model",
        )
        return
    if provider is not None and not isinstance(provider, str):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC configure command field provider must be a string",
        )
        return
    if model is not None and not isinstance(model, str):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC configure command field model must be a string",
        )
        return
    if isinstance(provider, str):
        try:
            agent.provider = runtime.providers.get(provider)
        except UnknownProviderError as exc:
            _write_rpc_command_error(
                command_id=command_id,
                command_type=command_type,
                message=str(exc),
            )
            return
        if not has_model:
            agent.model = None
    if has_model:
        agent.model = model
    _write_json_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


def _handle_rpc_approval_command(
    command: dict[str, object],
    *,
    command_id: str,
    command_type: str,
    approval_policy: _RpcToolApprovalPolicy,
) -> None:
    call_id = command.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC approval command requires string field: call_id",
        )
        return
    approved = command.get("approved")
    if not isinstance(approved, bool):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC approval command requires boolean field: approved",
        )
        return
    reason = command.get("reason")
    if reason is not None and not isinstance(reason, str):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC approval command field reason must be a string",
        )
        return
    if not approval_policy.resolve_approval(
        call_id=call_id,
        approved=approved,
        reason=reason,
    ):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=f"No pending tool approval with call_id: {call_id}",
        )
        return
    _write_json_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


def _handle_rpc_cancel_command(
    command: dict[str, object],
    *,
    command_id: str,
    command_type: str,
    running_prompt: _RpcRunningPrompt | None,
) -> None:
    target_id = command.get("target_id")
    if not isinstance(target_id, str) or not target_id:
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC cancel command requires string field: target_id",
        )
        return
    if running_prompt is None or running_prompt.command_id != target_id:
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=f"No running RPC command with id: {target_id}",
        )
        return
    running_prompt.cancel_scope.cancel()
    _write_json_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


def _write_rpc_command_error(*, command_id: str, command_type: str, message: str) -> None:
    _write_json_event(ErrorEvent(message=message))
    _write_json_event(
        RpcCommandFinished(
            command_id=command_id,
            command_type=command_type,
            ok=False,
            error=message,
        )
    )


def _rpc_command_identity(command: dict[str, object]) -> tuple[str, str, str | None]:
    command_type = _rpc_command_type(command)
    command_id, id_error = _rpc_command_id(command)
    return command_type, command_id, id_error


def _rpc_command_type(command: dict[str, object]) -> str:
    command_type = command.get("type")
    return command_type if isinstance(command_type, str) and command_type else "unknown"


def _rpc_command_id(command: dict[str, object]) -> tuple[str, str | None]:
    command_id = command.get("id")
    if command_id is None:
        return uuid4().hex, None
    if isinstance(command_id, str) and command_id:
        return command_id, None
    return uuid4().hex, "RPC command id must be a non-empty string"


def _parse_rpc_command(line: str) -> dict[str, object] | None:
    try:
        command = json.loads(line)
    except json.JSONDecodeError as exc:
        _write_json_event(ErrorEvent(message=f"Invalid RPC JSON: {exc.msg}"))
        return None
    if not isinstance(command, dict):
        _write_json_event(ErrorEvent(message="RPC command must be a JSON object"))
        return None
    return cast(dict[str, object], command)
