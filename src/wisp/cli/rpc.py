"""RPC output mode for Wisp's CLI.

This module hosts the JSONL RPC subsystem that was previously inlined in
``wisp.cli.__init__``. ``wisp.cli`` re-exports these names via compatibility
aliases so callers and tests keep importing them from ``wisp.cli``.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import cast
from uuid import uuid4

import anyio
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream

from wisp.agent.execution import ToolResultProcessingError
from wisp.agent.messages import Message
from wisp.agent.prompt import resolve_project_context_root
from wisp.coding import CodingSession, resolve_coding_session_configuration
from wisp.config import WispConfig
from wisp.events import (
    AgentStarted,
    ErrorEvent,
    ModelProviderAutoSwitched,
    RpcCommandFinished,
    RpcCommandStarted,
    SessionStatsReported,
    TrustRequested,
    TrustResolved,
    WispEvent,
)
from wisp.providers.base import Provider, ProviderError
from wisp.providers.catalog import AmbiguousModelError, UnknownModelError
from wisp.rpc.commands import ApprovalScope
from wisp.runtime.api import WispRuntime
from wisp.runtime.extensions import build_runtime
from wisp.runtime.registry import UnknownProviderError, UnknownToolError
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore, SessionError
from wisp.tools.approval import ToolApprovalDecision, ToolApprovalPolicy
from wisp.tools.base import Tool, ToolSafety
from wisp.trust import is_trusted, record_trust

from . import output as _cli_output
from . import tools as _cli_tools
from . import trust as _cli_trust
from .rpc_configuration import (
    RpcProjectConfiguration,
)
from .rpc_configuration import (
    _ConfigOverrides as _ConfigOverrides,
)
from .rpc_configuration import (
    _RpcConfigureOverrides as _RpcConfigureOverrides,
)
from .rpc_coordinator import (
    _MAX_QUEUED_RPC_COMMANDS as _MAX_QUEUED_RPC_COMMANDS,
)
from .rpc_coordinator import (
    RpcCoordinator,
)
from .rpc_coordinator import (
    _RpcCommandCompleted as _RpcCommandCompleted,
)
from .rpc_coordinator import (
    _RpcControlEvent as _RpcControlEvent,
)
from .rpc_coordinator import (
    _RpcDispatchResult as _RpcDispatchResult,
)
from .rpc_coordinator import (
    _RpcInputClosed as _RpcInputClosed,
)
from .rpc_coordinator import (
    _RpcInputCommand as _RpcInputCommand,
)
from .rpc_coordinator import (
    _RpcRunningCommand as _RpcRunningCommand,
)
from .rpc_coordinator import (
    _RpcSessionState as _RpcSessionState,
)
from .types import _JsonOutputModeError

_render_json_events = _cli_output._render_json_events
_write_json_event = _cli_output._write_json_event

_print_mode_tool_approval_policy = _cli_tools._print_mode_tool_approval_policy
_print_mode_tool_registry = _cli_tools._print_mode_tool_registry
_session_for_print_run = _cli_tools._session_for_print_run


async def _build_runtime_for_config(config: WispConfig) -> WispRuntime:
    # Preserve compatibility with embedders and tests that replace the runtime
    # factory with an older, narrower callable. Do not conceal TypeErrors raised
    # inside a compatible factory.
    for kwargs in (
        {"auth_path": config.auth_path, "retry_policy": config.retry_policy},
        {"auth_path": config.auth_path},
        {},
    ):
        try:
            return await build_runtime(**kwargs)
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc) or not kwargs:
                raise
    raise AssertionError("runtime factory compatibility loop exhausted")


@dataclass
class _RpcPendingApproval:
    call_id: str
    tool_name: str
    tool_safety: ToolSafety
    event: anyio.Event
    approved: bool | None = None
    reason: str | None = None
    resolved: bool = False


_STDIN_READ_CHUNK_SIZE = 64 * 1024
_STDIN_THREAD_POLL_INTERVAL = 0.01
_STDIN_THREAD_QUEUE_SIZE = 100


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
        pending = _RpcPendingApproval(
            call_id=call_id,
            tool_name=tool.name,
            tool_safety=tool.safety,
            event=anyio.Event(),
        )
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
        scope: ApprovalScope = "once",
    ) -> bool:
        pending = self._pending.get(call_id)
        if pending is None or pending.resolved:
            return False
        if approved and scope == "tool_session":
            self.approved_tools = self.approved_tools | {pending.tool_name}
        elif approved and scope == "all_session":
            self.approved_safety = self.approved_safety | {"mutating", "command"}
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


@dataclass
class _RpcPendingTrust:
    request_id: str
    event: anyio.Event
    trusted: bool | None = None
    reason: str | None = None
    transient: bool = False
    resolved: bool = False


class _RpcTrustGate:
    """Resolves project trust once, prompting the RPC client when undecided.

    Mirrors the approval policy's pending-request pattern: the project-trust
    question is a yes/no keyed by ``request_id`` that the client answers with a
    ``TrustCommand``. The decision is resolved lazily on first need (before the
    first prompt runs) and cached, so a project is prompted for at most once per
    process. Precedence: a ``WISP_TRUST`` override and a stored decision are
    honored without prompting; only an undecided project emits ``TrustRequested``
    and blocks. Input closing before an answer resolves to untrusted (safe).
    """

    def __init__(
        self,
        project_path: Path,
        *,
        on_first_trusted: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._project_path = project_path
        # Invoked at most once, the first time trust resolves to True, so a first-run
        # session that approves trust can rebuild the config-derived runtime with the
        # project's now-trusted ``.wisp/settings.json`` before the first turn runs.
        self._on_first_trusted = on_first_trusted
        self._pending: _RpcPendingTrust | None = None
        self._decision: bool | None = None
        self._resolved = False
        self._input_closed = False

    async def resolve(self) -> bool:
        """Return whether the project is trusted, prompting the client if needed."""

        if self._resolved:
            assert self._decision is not None
            return self._decision

        override = _cli_trust.trust_override_from_env()
        if override is not None:
            return await self._finish(override)

        stored = is_trusted(self._project_path)
        if stored is not None:
            return await self._finish(stored)

        if self._input_closed:
            return await self._finish(False)

        # Undecided: prompt the client. Only this path emits trust events, so a
        # project resolved by env override or a stored decision produces a clean
        # event stream.
        pending = _RpcPendingTrust(request_id=uuid4().hex, event=anyio.Event())
        self._pending = pending
        _write_json_event(
            TrustRequested(request_id=pending.request_id, project_path=self._project_path)
        )
        await pending.event.wait()
        trusted = pending.trusted is True
        if trusted:
            record_trust(self._project_path, True)
        elif not pending.transient:
            # Explicit "no" answers are persisted even when they carry explanatory
            # text. Forced UI/input-close denials opt into transient behavior.
            record_trust(self._project_path, False)
        self._pending = None
        _write_json_event(
            TrustResolved(
                request_id=pending.request_id,
                project_path=self._project_path,
                trusted=trusted,
                reason=pending.reason,
            )
        )
        return await self._finish(trusted)

    def resolve_request(
        self,
        *,
        request_id: str,
        trusted: bool,
        reason: str | None = None,
        transient: bool = False,
    ) -> bool:
        pending = self._pending
        if pending is None or pending.resolved or pending.request_id != request_id:
            return False
        pending.resolved = True
        pending.trusted = trusted
        pending.reason = reason
        pending.transient = transient
        pending.event.set()
        return True

    def deny_pending_on_input_closed(self) -> None:
        self._input_closed = True
        pending = self._pending
        if pending is not None and not pending.resolved:
            pending.resolved = True
            pending.trusted = False
            pending.reason = "RPC input closed before trust response"
            pending.transient = True
            pending.event.set()

    async def _finish(self, trusted: bool) -> bool:
        # Run the first-trusted rebuild BEFORE caching the decision. If the rebuild
        # raises (e.g. a trusted project's settings.json names an unknown provider), we
        # must NOT mark the gate resolved: otherwise the caller reports this prompt as
        # failed, but every later prompt would return the cached decision, skip the
        # rebuild, and silently run with the stale untrusted startup config. Leaving the
        # gate unresolved makes each subsequent prompt re-attempt the rebuild and fail
        # the same way, instead of quietly succeeding with the wrong provider.
        if trusted and self._on_first_trusted is not None and not self._resolved:
            await self._on_first_trusted()
        self._decision = trusted
        self._resolved = True
        return trusted


async def _run_rpc(
    config: WispConfig,
    all_tools: bool = False,
    allow_read_tools: bool = False,
    allowed_tools: tuple[str, ...] = (),
    resume: str | None = None,
    continue_latest: bool = False,
    approve_unsafe_tools: bool = False,
    max_tool_iterations: int | None = None,
    startup_trusted: bool = False,
    config_overrides: _ConfigOverrides | None = None,
    project_context_root: Path | None = None,
) -> None:
    runtime = await _build_runtime_for_config(config)
    sessions = JsonlSessionStore(config.session_dir)
    session = _session_for_print_run(sessions, resume=resume, continue_latest=continue_latest)
    session_state = _rpc_session_state(session)
    approval_policy = _RpcToolApprovalPolicy(_print_mode_tool_approval_policy(approve_unsafe_tools))
    selected_project_context_root = project_context_root or resolve_project_context_root(Path.cwd())
    configure_overrides = _RpcConfigureOverrides()
    project_configuration = RpcProjectConfiguration(
        startup_config=config,
        startup_trusted=startup_trusted,
        config_overrides=config_overrides,
        project_context_root=selected_project_context_root,
        runtime_builder=_build_runtime_for_config,
        configure_overrides=configure_overrides,
    )

    async def _rebuild_agent_for_trusted_project() -> None:
        event = await project_configuration.apply_trusted_project(runtime=runtime, agent=agent)
        if event is not None:
            _write_json_event(event)

    trust_gate = _RpcTrustGate(
        selected_project_context_root,
        on_first_trusted=_rebuild_agent_for_trusted_project,
    )
    initial_configuration = resolve_coding_session_configuration(
        config,
        providers=runtime.providers,
        models=runtime.models,
        trusted=startup_trusted,
    )
    agent = CodingSession.from_configuration(
        initial_configuration,
        sessions=sessions,
        events=runtime.events,
        tool_registry=_print_mode_tool_registry(
            runtime.tools,
            all_tools=all_tools,
            allow_read_tools=allow_read_tools,
            allowed_tools=allowed_tools,
        ),
        tool_approval_policy=approval_policy,
        max_tool_iterations=max_tool_iterations,
        project_context_root=selected_project_context_root,
    )

    coordinator = RpcCoordinator(
        session_state,
        input_closed_handlers=(
            approval_policy.deny_pending_on_input_closed,
            trust_gate.deny_pending_on_input_closed,
        ),
        max_queued_commands=_MAX_QUEUED_RPC_COMMANDS,
        input_closed_type=_RpcInputClosed,
        command_completed_type=_RpcCommandCompleted,
    )
    send, receive = anyio.create_memory_object_stream[_RpcControlEvent](100)
    stop_reader = anyio.Event()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(_read_rpc_stdin, send.clone(), stop_reader)
        async with send, receive:

            def dispatch(
                command: dict[str, object],
                running_command: _RpcRunningCommand | None,
            ) -> _RpcDispatchResult:
                return _dispatch_rpc_command(
                    command,
                    agent=agent,
                    runtime=runtime,
                    sessions=sessions,
                    session_state=session_state,
                    task_group=task_group,
                    send=send,
                    running_command=running_command,
                    approval_policy=approval_policy,
                    trust_gate=trust_gate,
                    configure_overrides=configure_overrides,
                    queued_commands=coordinator.queued_commands,
                )

            await coordinator.run(
                receive,
                dispatch=dispatch,
                reject=lambda command, message: _reject_rpc_command(
                    command,
                    message=message,
                ),
                command_type=_rpc_command_type,
            )
            stop_reader.set()
            task_group.cancel_scope.cancel()
            return


def _dispatch_rpc_command(
    command: dict[str, object],
    *,
    agent: CodingSession,
    runtime: WispRuntime,
    sessions: JsonlSessionStore,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    running_command: _RpcRunningCommand | None,
    approval_policy: _RpcToolApprovalPolicy,
    trust_gate: _RpcTrustGate,
    configure_overrides: _RpcConfigureOverrides,
    queued_commands: deque[dict[str, object]],
) -> _RpcDispatchResult:
    command_type = _rpc_command_type(command)
    if command_type == "prompt":
        new_running_command, new_session = _start_rpc_prompt_command(
            command,
            agent=agent,
            sessions=sessions,
            session_state=session_state,
            task_group=task_group,
            send=send,
            trust_gate=trust_gate,
        )
        return _RpcDispatchResult(
            running_command=new_running_command,
            selected_session=new_session,
        )
    if command_type == "compact":
        return _RpcDispatchResult(
            running_command=_start_rpc_compact_command(
                command,
                agent=agent,
                session_state=session_state,
                task_group=task_group,
                send=send,
                trust_gate=trust_gate,
            )
        )
    if command_type == "get_session_stats":
        return _RpcDispatchResult(
            running_command=_start_rpc_session_stats_command(
                command,
                agent=agent,
                session_state=session_state,
                task_group=task_group,
                send=send,
            )
        )
    should_shutdown = _handle_rpc_control_command(
        command,
        agent=agent,
        runtime=runtime,
        running_command=running_command,
        approval_policy=approval_policy,
        trust_gate=trust_gate,
        configure_overrides=configure_overrides,
        queued_commands=queued_commands,
    )
    return _RpcDispatchResult(
        running_command=running_command,
        should_shutdown=should_shutdown,
    )


def _rpc_session_state(session: JsonlSession | None) -> _RpcSessionState:
    if session is None or not session.path.is_file():
        return _RpcSessionState(session=session, history=(), entry_count=0)
    return _RpcSessionState(
        session=session,
        history=session.read_context_messages(),
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
    agent: CodingSession,
    sessions: JsonlSessionStore,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    trust_gate: _RpcTrustGate,
) -> tuple[_RpcRunningCommand | None, JsonlSession | None]:
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
        trust_gate,
    )
    return (
        _RpcRunningCommand(
            command_id=command_id,
            command_type="prompt",
            cancel_scope=cancel_scope,
        ),
        selected_session,
    )


def _start_rpc_compact_command(
    command: dict[str, object],
    *,
    agent: CodingSession,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    trust_gate: _RpcTrustGate,
) -> _RpcRunningCommand | None:
    command_type, command_id, id_error = _rpc_command_identity(command)
    _write_json_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
        )
        return None

    raw_instructions = command.get("instructions")
    if raw_instructions is not None and not isinstance(raw_instructions, str):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC compact command field instructions must be a string",
        )
        return None
    instructions = raw_instructions.strip() or None if isinstance(raw_instructions, str) else None

    session = session_state.session
    if session is None or not session.path.is_file():
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC compact command requires an existing persisted session",
        )
        return None

    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        _run_rpc_compact_command,
        agent,
        session,
        session_state.history,
        session_state.entry_count,
        instructions,
        command_id,
        cancel_scope,
        send.clone(),
        trust_gate,
    )
    return _RpcRunningCommand(
        command_id=command_id,
        command_type="compact",
        cancel_scope=cancel_scope,
    )


def _start_rpc_session_stats_command(
    command: dict[str, object],
    *,
    agent: CodingSession,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
) -> _RpcRunningCommand | None:
    command_type, command_id, id_error = _rpc_command_identity(command)
    _write_json_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
        )
        return None

    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        _run_rpc_session_stats_command,
        agent,
        session_state.session,
        session_state.entry_count,
        command_id,
        cancel_scope,
        send.clone(),
    )
    return _RpcRunningCommand(
        command_id=command_id,
        command_type="get_session_stats",
        cancel_scope=cancel_scope,
    )


async def _run_rpc_session_stats_command(
    agent: CodingSession,
    session: JsonlSession | None,
    entry_count: int,
    command_id: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
) -> None:
    ok = False
    error: str | None = None
    refreshed_history: tuple[Message, ...] | None = None
    refreshed_entry_count = entry_count
    try:
        with cancel_scope:
            stats = await agent.get_session_stats(session)
            if session is not None:
                refreshed_entry_count, refreshed_history = await anyio.to_thread.run_sync(
                    _updated_rpc_session_state,
                    session,
                    (),
                    entry_count,
                )
            _write_json_event(SessionStatsReported(command_id=command_id, stats=stats))
            ok = True
        if cancel_scope.cancel_called:
            error = "RPC get_session_stats command cancelled"
    except BaseException as exc:
        if isinstance(exc, anyio.get_cancelled_exc_class()):
            error = "RPC get_session_stats command cancelled"
        else:
            error = str(exc)
    finally:
        _write_json_event(
            RpcCommandFinished(
                command_id=command_id,
                command_type="get_session_stats",
                ok=ok,
                error=error,
            )
        )
        await send.send(
            _RpcCommandCompleted(
                command_id=command_id,
                command_type="get_session_stats",
                ok=ok,
                history=refreshed_history,
                entry_count=refreshed_entry_count,
            )
        )
        await send.aclose()


async def _run_rpc_prompt_command(
    agent: CodingSession,
    session: JsonlSession,
    committed_history: tuple[Message, ...],
    entry_start: int,
    prompt: str,
    command_id: str,
    command_type: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    trust_gate: _RpcTrustGate,
) -> None:
    error: str | None = None
    run_entry_start = entry_start

    async def track_run_start(events: AsyncIterator[WispEvent]) -> AsyncIterator[WispEvent]:
        nonlocal run_entry_start
        async for event in events:
            if isinstance(event, AgentStarted):
                # AgentStarted follows recovery of prior pending entries and precedes
                # persistence of this run's prompt messages.
                run_entry_start = await anyio.to_thread.run_sync(
                    _rpc_session_entry_count,
                    session,
                    entry_start,
                )
            yield event

    try:
        with cancel_scope:
            try:
                # Resolve project trust before the first turn runs; the decision is
                # cached so subsequent prompts don't re-prompt. Trust commands are
                # processed by the main loop while this awaits, so this cannot
                # deadlock the reader.
                agent.trusted = await trust_gate.resolve()
                await _render_json_events(
                    track_run_start(
                        agent.run(
                            prompt,
                            session=session,
                            history=committed_history,
                            operation_id=command_id,
                        )
                    )
                )
            except _JsonOutputModeError as exc:
                error = str(exc)
            except (
                ProviderError,
                SessionError,
                ToolResultProcessingError,
                UnknownProviderError,
                UnknownToolError,
            ) as exc:
                # Runtime/configuration failures and internal result-processing errors
                # fail this command without tearing down the RPC process. The agent loop
                # already emitted the safe ErrorEvent for failures raised during a run.
                error = str(exc)
            except anyio.get_cancelled_exc_class():
                error = f"RPC command cancelled: {command_id}"
    finally:
        cancelled = error is not None and error.startswith("RPC command cancelled:")
        if cancelled:
            # Keep recovered prior entries, but discard this prompt unless the run
            # produced provider output that CodingSession durably persisted.
            crossed_completion_boundary = await anyio.to_thread.run_sync(
                _rpc_has_durable_completion,
                session,
                run_entry_start,
                command_id,
            )
            if not crossed_completion_boundary:
                rolled_back = await session.truncate_operation_entries(
                    run_entry_start,
                    operation_id=command_id,
                )
                if not rolled_back and session.path.is_file():
                    entries = await anyio.to_thread.run_sync(session.read_entries)
                    if any(entry.operation_id == command_id for entry in entries[run_entry_start:]):
                        error = (
                            f"RPC command cancelled: {command_id}; prompt entries were retained "
                            "because another writer appended to the session"
                        )
        entry_count, updated_history = await anyio.to_thread.run_sync(
            _updated_rpc_session_state,
            session,
            committed_history,
            entry_start,
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
                _RpcCommandCompleted(
                    command_id=command_id,
                    command_type="prompt",
                    ok=error is None,
                    history=updated_history,
                    entry_count=entry_count,
                )
            )


async def _run_rpc_compact_command(
    agent: CodingSession,
    session: JsonlSession,
    committed_history: tuple[Message, ...],
    entry_start: int,
    instructions: str | None,
    command_id: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    trust_gate: _RpcTrustGate,
) -> None:
    error: str | None = None
    error_rendered = False

    async def track_errors(events: AsyncIterator[WispEvent]) -> AsyncIterator[WispEvent]:
        nonlocal error_rendered
        async for event in events:
            if isinstance(event, ErrorEvent):
                error_rendered = True
            yield event

    try:
        with cancel_scope:
            try:
                agent.trusted = await trust_gate.resolve()
                await _render_json_events(
                    track_errors(agent.compact(session, instructions=instructions))
                )
            except _JsonOutputModeError as exc:
                error = str(exc)
                error_rendered = True
            except anyio.get_cancelled_exc_class():
                error = f"RPC command cancelled: {command_id}"
            except Exception as exc:  # noqa: BLE001 - command failures must not stop RPC
                error = str(exc)
    finally:
        try:
            entry_count, updated_history = await anyio.to_thread.run_sync(
                _updated_rpc_session_state,
                session,
                committed_history,
                entry_start,
            )
        except Exception as exc:  # noqa: BLE001 - preserve a usable RPC coordinator
            entry_count = entry_start
            updated_history = committed_history
            if error is None:
                error = str(exc)
        async with send:
            if error is not None and not error_rendered:
                _write_json_event(ErrorEvent(message=error))
            _write_json_event(
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="compact",
                    ok=error is None,
                    error=error,
                )
            )
            await send.send(
                _RpcCommandCompleted(
                    command_id=command_id,
                    command_type="compact",
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
    return _updated_rpc_session_state(session, committed_history, entry_start)[1]


def _rpc_session_entry_count(
    session: JsonlSession,
    fallback: int,
) -> int:
    if not session.path.is_file():
        return fallback
    return len(session.read_entries())


def _rpc_has_durable_completion(
    session: JsonlSession,
    entry_start: int,
    operation_id: str,
) -> bool:
    if not session.path.is_file():
        return False
    for entry in session.read_entries()[entry_start:]:
        if entry.operation_id != operation_id:
            continue
        message = entry.message
        if message is None:
            continue
        if message.role == "assistant" and message.finish_reason is not None:
            return True
        if message.role == "tool" and message.tool_call_id is not None:
            return True
    return False


def _updated_rpc_session_state(
    session: JsonlSession,
    committed_history: tuple[Message, ...],
    entry_start: int,
) -> tuple[int, tuple[Message, ...]]:
    if not session.path.is_file():
        return entry_start, committed_history
    entries = session.read_entries()
    return len(entries), session.read_context_messages()


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
    running_command: _RpcRunningCommand | None,
    approval_policy: _RpcToolApprovalPolicy,
    agent: CodingSession | None = None,
    runtime: WispRuntime | None = None,
    trust_gate: _RpcTrustGate | None = None,
    configure_overrides: _RpcConfigureOverrides | None = None,
    queued_commands: deque[dict[str, object]] | None = None,
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
            running_command=running_command,
            queued_commands=queued_commands,
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
    if command_type == "trust":
        if trust_gate is None:
            _write_rpc_command_error(
                command_id=command_id,
                command_type=command_type,
                message="RPC trust command requires an active trust gate",
            )
            return False
        _handle_rpc_trust_command(
            command,
            command_id=command_id,
            command_type=command_type,
            trust_gate=trust_gate,
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
            configure_overrides=configure_overrides,
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
    agent: CodingSession,
    runtime: WispRuntime,
    configure_overrides: _RpcConfigureOverrides | None = None,
) -> None:
    provider = command.get("provider")
    model = command.get("model")
    effort = command.get("effort")
    clear_effort = command.get("clear_effort") is True
    has_provider = "provider" in command
    has_model = "model" in command
    has_effort = "effort" in command or clear_effort
    if not has_provider and not has_model and not has_effort:
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC configure command requires provider, model, or effort",
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
    if effort is not None and not isinstance(effort, str):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC configure command field effort must be a string",
        )
        return
    configuration = agent.configuration
    selected_provider = configuration.provider
    selected_model = configuration.model
    selected_effort = configuration.effort
    if isinstance(provider, str):
        try:
            selected_provider = runtime.providers.get(provider)
        except UnknownProviderError as exc:
            _write_rpc_command_error(
                command_id=command_id,
                command_type=command_type,
                message=str(exc),
            )
            return
        if configure_overrides is not None:
            configure_overrides.provider = provider
        if not has_model:
            selected_model = None
            if configure_overrides is not None:
                configure_overrides.model = None
                configure_overrides.has_model = True
        if not has_effort:
            # Effort tiers are provider-native, non-normalized strings (e.g.
            # Google's "MEDIUM" vs OpenAI's lowercase "medium") -- a tier
            # chosen for the old provider left in place across a provider
            # switch would reach the new provider's API unvalidated on the
            # next prompt, matching the same staleness model already gets
            # reset for above.
            selected_effort = None
            if configure_overrides is not None:
                configure_overrides.effort = None
                configure_overrides.has_effort = True
    if has_model and provider is None and isinstance(model, str):
        selected_provider = _auto_switch_provider_for_model(
            model,
            command_id=command_id,
            current_provider=selected_provider,
            runtime=runtime,
            configure_overrides=configure_overrides,
        )
        if not has_effort:
            # Effort support is per-model, not just per-provider --
            # catalog.toml deliberately omits some models (e.g.
            # claude-haiku-4-5) from effort_levels entirely. A model change
            # that stays on the same provider skips both the explicit-
            # provider reset above and _auto_switch_provider_for_model's own
            # reset (it only fires when the provider actually changes), so a
            # tier valid for the old model could be unsupported by the new
            # one -- reset rather than risk sending an incompatible
            # output_config.effort (or equivalent) on the next prompt.
            selected_effort = None
            if configure_overrides is not None:
                configure_overrides.effort = None
                configure_overrides.has_effort = True
    if has_model:
        selected_model = model
        if configure_overrides is not None:
            configure_overrides.model = model
            configure_overrides.has_model = True
    if has_effort:
        selected_effort = None if clear_effort else effort
        if configure_overrides is not None:
            configure_overrides.effort = None if clear_effort else effort
            configure_overrides.has_effort = True
    agent.reconfigure(
        replace(
            configuration,
            provider=selected_provider,
            model=selected_model,
            effort=selected_effort,
            models=runtime.models,
        )
    )
    _write_json_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


def _auto_switch_provider_for_model(
    model: str,
    *,
    command_id: str,
    current_provider: Provider,
    runtime: WispRuntime,
    configure_overrides: _RpcConfigureOverrides | None,
) -> Provider:
    """Return the provider selected for ``model`` when it is unambiguous.

    Advisory, never blocking: an unknown or ambiguous model id is left entirely
    to the existing free-text ``agent.model = model`` assignment -- this must
    never reject a model string that would have worked before the registry
    existed (a brand-new model ahead of a catalog update, a custom provider, or
    a model shared by two providers while already on one of them).

    Emits :class:`ModelProviderAutoSwitched` before the switch is applied so an
    out-of-process front-end (the TUI) that only tracks provider changes it
    explicitly requested can resync -- otherwise it would keep displaying and
    using its old provider while the RPC agent has actually moved on.
    """

    try:
        resolved_provider, _entry = runtime.models.resolve(model, prefer=current_provider.name)
    except (UnknownModelError, AmbiguousModelError):
        return current_provider
    if resolved_provider == current_provider.name:
        return current_provider
    try:
        new_provider = runtime.providers.get(resolved_provider)
    except UnknownProviderError:
        return current_provider
    _write_json_event(
        ModelProviderAutoSwitched(command_id=command_id, provider=resolved_provider, model=model)
    )
    if configure_overrides is not None:
        configure_overrides.provider = resolved_provider
    return new_provider


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
    raw_scope = command.get("scope")
    scope: ApprovalScope
    if raw_scope is None:
        scope = "once"
    elif isinstance(raw_scope, str) and raw_scope in {
        "once",
        "tool_session",
        "all_session",
    }:
        scope = cast(ApprovalScope, raw_scope)
    else:
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=(
                "RPC approval command field scope must be one of: once, tool_session, all_session"
            ),
        )
        return
    if not approved and scope != "once":
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC approval scope is only valid for approved requests",
        )
        return
    if not approval_policy.resolve_approval(
        call_id=call_id,
        approved=approved,
        reason=reason,
        scope=scope,
    ):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=f"No pending tool approval with call_id: {call_id}",
        )
        return
    _write_json_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


def _handle_rpc_trust_command(
    command: dict[str, object],
    *,
    command_id: str,
    command_type: str,
    trust_gate: _RpcTrustGate,
) -> None:
    request_id = command.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC trust command requires string field: request_id",
        )
        return
    trusted = command.get("trusted")
    if not isinstance(trusted, bool):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC trust command requires boolean field: trusted",
        )
        return
    reason = command.get("reason")
    if reason is not None and not isinstance(reason, str):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC trust command field reason must be a string",
        )
        return
    transient = command.get("transient")
    if transient is not None and not isinstance(transient, bool):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC trust command field transient must be a boolean",
        )
        return
    if not trust_gate.resolve_request(
        request_id=request_id,
        trusted=trusted,
        reason=reason,
        transient=transient is True,
    ):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=f"No pending trust request with request_id: {request_id}",
        )
        return
    _write_json_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


def _handle_rpc_cancel_command(
    command: dict[str, object],
    *,
    command_id: str,
    command_type: str,
    running_command: _RpcRunningCommand | None,
    queued_commands: deque[dict[str, object]] | None = None,
) -> None:
    target_id = command.get("target_id")
    if not isinstance(target_id, str) or not target_id:
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC cancel command requires string field: target_id",
        )
        return
    if running_command is not None and running_command.command_id == target_id:
        running_command.cancel_scope.cancel()
        _write_json_event(
            RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True)
        )
        return

    queued_target = next(
        (queued for queued in queued_commands or () if queued.get("id") == target_id),
        None,
    )
    if queued_target is None:
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=f"No running or queued RPC command with id: {target_id}",
        )
        return
    assert queued_commands is not None
    queued_commands.remove(queued_target)
    target_type = _rpc_command_type(queued_target)
    _write_json_event(RpcCommandStarted(command_id=target_id, command_type=target_type))
    _write_json_event(
        RpcCommandFinished(
            command_id=target_id,
            command_type=target_type,
            ok=False,
            error=f"RPC command cancelled: {target_id}",
        )
    )
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
