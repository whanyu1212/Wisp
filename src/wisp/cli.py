"""Command-line interface for Wisp."""

from __future__ import annotations

import json
import os
import stat
import sys
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
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
from wisp.config import WispConfig
from wisp.events import (
    ErrorEvent,
    RpcCommandFinished,
    RpcCommandStarted,
    SessionSaved,
    TokenDelta,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolResultReady,
    WispEvent,
)
from wisp.providers.base import ProviderError
from wisp.runtime.extensions import build_runtime
from wisp.runtime.registry import ToolRegistry, UnknownProviderError, UnknownToolError
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore, SessionError
from wisp.tools.approval import ToolApprovalPolicy


class OutputMode(StrEnum):
    """Non-interactive output modes."""

    text = "text"
    json = "json"
    rpc = "rpc"


class _JsonOutputModeError(ProviderError):
    """Raised after JSONL output has already emitted a model-visible error event."""


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


type _RpcControlEvent = _RpcInputCommand | _RpcInputClosed | _RpcPromptCompleted


_STDIN_READ_CHUNK_SIZE = 64 * 1024
_STDIN_THREAD_POLL_INTERVAL = 0.01
_MAX_QUEUED_RPC_COMMANDS = 100


app = typer.Typer(
    add_completion=False,
    help="Wisp: a Python, Pi-inspired coding agent.",
    invoke_without_command=True,
    no_args_is_help=True,
)


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
    mode: Annotated[
        OutputMode,
        typer.Option("--mode", help="Output mode: text, JSONL events, or RPC."),
    ] = OutputMode.text,
    allow_read_tools: Annotated[
        bool,
        typer.Option(help="Expose sandboxed read-only tools in print mode."),
    ] = False,
    allow_tool: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-tool",
            help="Expose a specific tool in print mode. Can be repeated.",
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
    if mode is OutputMode.rpc:
        if prompt is not None:
            _exit_with_error(
                "--prompt is not used with --mode rpc; send prompt commands on stdin",
                mode=mode,
                console=console,
            )
    elif prompt is None:
        # no_args_is_help normally handles this. This branch keeps direct calls
        # to the callback friendly in tests or embedded usage.
        raise typer.Exit(0)

    if resume is not None and continue_latest:
        _exit_with_error(
            "use either --resume or --continue, not both",
            mode=mode,
            console=console,
        )
    if max_tool_iterations is not None and max_tool_iterations < 0:
        _exit_with_error(
            "--max-tool-iterations must be non-negative",
            mode=mode,
            console=console,
        )

    config = WispConfig.from_env(provider=provider, model=model, session_dir=session_dir)
    try:
        if mode is OutputMode.rpc:
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
                mode,
            )
    except _JsonOutputModeError as exc:
        raise typer.Exit(1) from exc
    except (ProviderError, SessionError, UnknownProviderError, UnknownToolError) as exc:
        if _writes_json_events(mode):
            _write_json_event(ErrorEvent(message=str(exc)))
        else:
            console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1) from exc


def main() -> None:
    """Console-script entry point."""

    app()


def _exit_with_error(message: str, *, mode: OutputMode, console: Console) -> None:
    if _writes_json_events(mode):
        _write_json_event(ErrorEvent(message=message))
    else:
        console.print(f"[red]error:[/red] {message}")
    raise typer.Exit(1)


def _writes_json_events(mode: OutputMode) -> bool:
    return mode in {OutputMode.json, OutputMode.rpc}


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
    runtime = await build_runtime()
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
    runtime = await build_runtime()
    provider = runtime.providers.get(config.provider)
    sessions = JsonlSessionStore(config.session_dir)
    session = _session_for_print_run(sessions, resume=resume, continue_latest=continue_latest)
    session_state = _rpc_session_state(session)
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
                        sessions=sessions,
                        session_state=session_state,
                        task_group=task_group,
                        send=send,
                        running_prompt=running_prompt,
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
                if running_prompt is not None and command_type != "cancel":
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
                    sessions=sessions,
                    session_state=session_state,
                    task_group=task_group,
                    send=send,
                    running_prompt=running_prompt,
                )
                if should_shutdown:
                    stop_reader.set()
                    task_group.cancel_scope.cancel()
                    return


def _dispatch_rpc_command(
    command: dict[str, object],
    *,
    agent: Agent,
    sessions: JsonlSessionStore,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    running_prompt: _RpcRunningPrompt | None,
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
    should_shutdown = _handle_rpc_control_command(command, running_prompt=running_prompt)
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
    lines: Queue[str | Exception] = Queue()
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
    message = f"Unknown RPC command: {command_type}"
    _write_rpc_command_error(command_id=command_id, command_type=command_type, message=message)
    return False


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


async def _render_json_events(events: AsyncIterator[WispEvent]) -> None:
    rendered_error: str | None = None
    try:
        async for event in events:
            _write_json_event(event)
            if isinstance(event, ErrorEvent):
                rendered_error = event.message
    except Exception as exc:
        if rendered_error is None:
            rendered_error = str(exc)
            _write_json_event(ErrorEvent(message=rendered_error))
        raise _JsonOutputModeError(rendered_error) from exc


def _write_json_event(event: WispEvent) -> None:
    sys.stdout.write(f"{event.model_dump_json()}\n")
    sys.stdout.flush()


def _render_print_event(event: WispEvent, console: Console) -> None:
    line = _print_event_line(event)
    if line is not None:
        console.print(line, markup=False)


def _print_event_line(event: WispEvent) -> str | None:
    if isinstance(event, ToolCallRequested):
        return f"→ tool {event.name} {_format_event_arguments(event.arguments)}"
    if isinstance(event, ToolApprovalRequested):
        return f"? approval required for {event.name} ({event.safety})"
    if isinstance(event, ToolApprovalResolved):
        if event.approved:
            return f"✓ approved {event.name}"
        reason = f": {event.reason}" if event.reason else ""
        return f"! denied {event.name}{reason}"
    if isinstance(event, ToolResultReady):
        status = "✗" if event.is_error else "✓"
        return f"{status} tool {event.name}: {_format_event_output(event.output)}"
    if isinstance(event, SessionSaved):
        return f"session saved: {event.path}"
    return None


def _format_event_arguments(arguments: dict[str, object]) -> str:
    if not arguments:
        return "{}"
    try:
        text = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(arguments)
    return _truncate_inline(text, 240)


def _format_event_output(output: str) -> str:
    first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
    return _truncate_inline(first_line or "(no output)", 240)


def _truncate_inline(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[: max(0, max_chars - 1)].rstrip()}…"


def _print_mode_tool_approval_policy(approve_unsafe_tools: bool) -> ToolApprovalPolicy:
    if approve_unsafe_tools:
        return ToolApprovalPolicy.approve_all()
    return ToolApprovalPolicy.require_approval()


def _session_for_print_run(
    sessions: JsonlSessionStore,
    *,
    resume: str | None,
    continue_latest: bool,
) -> JsonlSession | None:
    if resume is not None:
        return sessions.load(resume)
    if continue_latest:
        return sessions.latest()
    return None


def _print_mode_tool_registry(
    tools: ToolRegistry,
    *,
    allow_read_tools: bool = False,
    allowed_tools: tuple[str, ...] = (),
) -> ToolRegistry:
    """Return tools explicitly allowed for non-interactive print mode."""

    allowed_names = set(allowed_tools)
    for name in allowed_names:
        tools.get(name)

    filtered = ToolRegistry()
    for tool in tools.all():
        if tool.name in allowed_names or (allow_read_tools and tool.safety == "read"):
            filtered.register(tool)
    return filtered
