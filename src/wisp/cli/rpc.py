"""RPC output mode for Wisp's CLI.

This module hosts the JSONL RPC subsystem that was previously inlined in
``wisp.cli.__init__``. ``wisp.cli`` re-exports these names via compatibility
aliases so callers and tests keep importing them from ``wisp.cli``.
"""

from __future__ import annotations

import os
import sys
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from threading import Thread
from uuid import uuid4

import anyio
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream

from wisp.agent.messages import Message
from wisp.agent.prompt import resolve_project_context_root
from wisp.coding import CodingSession, resolve_coding_session_configuration
from wisp.config import WispConfig
from wisp.events import (
    TrustRequested,
    TrustResolved,
)
from wisp.providers.base import Provider
from wisp.rpc.commands import ApprovalScope
from wisp.runtime.api import WispRuntime
from wisp.runtime.extensions import build_runtime
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore
from wisp.tools.approval import ToolApprovalDecision, ToolApprovalPolicy
from wisp.tools.base import Tool, ToolSafety
from wisp.trust import is_trusted, record_trust

from . import output as _cli_output
from . import rpc_execution as _rpc_execution
from . import rpc_transport as _rpc_transport
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
    _RpcPromptReady as _RpcPromptReady,
)
from .rpc_coordinator import (
    _RpcRunningCommand as _RpcRunningCommand,
)
from .rpc_coordinator import (
    _RpcSessionState as _RpcSessionState,
)

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


_STDIN_READ_CHUNK_SIZE = _rpc_transport._STDIN_READ_CHUNK_SIZE
_STDIN_THREAD_POLL_INTERVAL = _rpc_transport._STDIN_THREAD_POLL_INTERVAL
_STDIN_THREAD_QUEUE_SIZE = _rpc_transport._STDIN_THREAD_QUEUE_SIZE


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
        completion_event_writer=_write_json_event,
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
                    coordinator=coordinator,
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
    coordinator: RpcCoordinator | None = None,
    queued_commands: deque[dict[str, object]] | None = None,
) -> _RpcDispatchResult:
    if coordinator is None:
        coordinator = RpcCoordinator(session_state, completion_event_writer=_write_json_event)
        coordinator.running_command = running_command
        if queued_commands is not None:
            coordinator.queued_commands = queued_commands
    executor = _rpc_execution.RpcCommandExecutor(
        agent=agent,
        runtime=runtime,
        sessions=sessions,
        session_state=session_state,
        task_group=task_group,
        send=send,
        approval_policy=approval_policy,
        trust_gate=trust_gate,
        configure_overrides=configure_overrides,
        coordinator=coordinator,
        write_event=_write_json_event,
        render_events=_render_json_events,
        running_command_factory=_RpcRunningCommand,
        command_completed_factory=_RpcCommandCompleted,
    )
    return executor.dispatch(command, running_command)


def _rpc_session_state(session: JsonlSession | None) -> _RpcSessionState:
    return _rpc_execution.rpc_session_state(session)


async def _read_rpc_stdin(
    send: MemoryObjectSendStream[_RpcControlEvent],
    stop_reader: anyio.Event,
) -> None:
    await _rpc_stdin_transport().read(send, stop_reader)


def _rpc_stdin_needs_thread_reader(stdin_mode: int) -> bool:
    return _rpc_transport.rpc_stdin_needs_thread_reader(stdin_mode)


async def _read_rpc_text_stdin(
    send: MemoryObjectSendStream[_RpcControlEvent],
    stop_reader: anyio.Event,
) -> None:
    await _rpc_stdin_transport().read_text(send, stop_reader)


async def _read_rpc_thread_stdin(
    send: MemoryObjectSendStream[_RpcControlEvent],
    stop_reader: anyio.Event,
) -> None:
    await _rpc_stdin_transport().read_thread(send, stop_reader)


async def _read_rpc_fd_stdin(
    send: MemoryObjectSendStream[_RpcControlEvent],
    stop_reader: anyio.Event,
    fd: int,
) -> None:
    await _rpc_stdin_transport().read_fd(send, stop_reader, fd)


async def _send_rpc_input_line(
    send: MemoryObjectSendStream[_RpcControlEvent],
    raw_line: str,
) -> None:
    await _rpc_stdin_transport().send_line(send, raw_line)


def _decode_rpc_stdin_line(raw_line: bytes | bytearray) -> str:
    return _rpc_transport.decode_rpc_stdin_line(raw_line)


def _rpc_stdin_transport() -> _rpc_transport.RpcStdinTransport[_RpcControlEvent]:
    return _rpc_transport.RpcStdinTransport(
        stdin=sys.stdin,
        write_event=_write_json_event,
        input_command_factory=lambda command: _RpcInputCommand(command=command),
        input_closed_factory=_RpcInputClosed,
        queue_factory=lambda maxsize: Queue(maxsize=maxsize),
        thread_factory=Thread,
        wait_readable=anyio.wait_readable,
        read_fd=os.read,
        needs_thread_reader=_rpc_stdin_needs_thread_reader,
        read_chunk_size=_STDIN_READ_CHUNK_SIZE,
        thread_poll_interval=_STDIN_THREAD_POLL_INTERVAL,
        thread_queue_size=_STDIN_THREAD_QUEUE_SIZE,
    )


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
    return _rpc_execution.start_rpc_prompt_command(
        command,
        agent=agent,
        sessions=sessions,
        session_state=session_state,
        task_group=task_group,
        send=send,
        trust_gate=trust_gate,
        write_event=_write_json_event,
        render_events=_render_json_events,
        running_command_factory=_RpcRunningCommand,
        command_completed_factory=_RpcCommandCompleted,
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
    return _rpc_execution.start_rpc_compact_command(
        command,
        agent=agent,
        session_state=session_state,
        task_group=task_group,
        send=send,
        trust_gate=trust_gate,
        write_event=_write_json_event,
        render_events=_render_json_events,
        running_command_factory=_RpcRunningCommand,
        command_completed_factory=_RpcCommandCompleted,
    )


def _start_rpc_session_stats_command(
    command: dict[str, object],
    *,
    agent: CodingSession,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
) -> _RpcRunningCommand | None:
    return _rpc_execution.start_rpc_session_stats_command(
        command,
        agent=agent,
        session_state=session_state,
        task_group=task_group,
        send=send,
        write_event=_write_json_event,
        running_command_factory=_RpcRunningCommand,
        command_completed_factory=_RpcCommandCompleted,
    )


async def _run_rpc_session_stats_command(
    agent: CodingSession,
    session: JsonlSession | None,
    entry_count: int,
    command_id: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
) -> None:
    await _rpc_execution.run_rpc_session_stats_command(
        agent,
        session,
        entry_count,
        command_id,
        cancel_scope,
        send,
        _write_json_event,
        _RpcCommandCompleted,
    )


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
    await _rpc_execution.run_rpc_prompt_command(
        agent,
        session,
        committed_history,
        entry_start,
        prompt,
        command_id,
        command_type,
        cancel_scope,
        send,
        trust_gate,
        _write_json_event,
        _render_json_events,
        _RpcCommandCompleted,
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
    await _rpc_execution.run_rpc_compact_command(
        agent,
        session,
        committed_history,
        entry_start,
        instructions,
        command_id,
        cancel_scope,
        send,
        trust_gate,
        _write_json_event,
        _render_json_events,
        _RpcCommandCompleted,
    )


def _updated_rpc_history(
    session: JsonlSession,
    committed_history: tuple[Message, ...],
    entry_start: int,
) -> tuple[Message, ...]:
    return _rpc_execution.updated_rpc_history(session, committed_history, entry_start)


def _rpc_session_entry_count(
    session: JsonlSession,
    fallback: int,
) -> int:
    return _rpc_execution.rpc_session_entry_count(session, fallback)


def _rpc_has_durable_completion(
    session: JsonlSession,
    entry_start: int,
    operation_id: str,
) -> bool:
    return _rpc_execution.rpc_has_durable_completion(session, entry_start, operation_id)


def _updated_rpc_session_state(
    session: JsonlSession,
    committed_history: tuple[Message, ...],
    entry_start: int,
) -> tuple[int, tuple[Message, ...]]:
    return _rpc_execution.updated_rpc_session_state(session, committed_history, entry_start)


def _reject_rpc_command(command: dict[str, object], *, message: str) -> None:
    _rpc_execution.reject_rpc_command(
        command,
        message=message,
        write_event=_write_json_event,
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
    coordinator: RpcCoordinator | None = None,
    queued_commands: deque[dict[str, object]] | None = None,
) -> bool:
    return _rpc_execution.handle_rpc_control_command(
        command,
        running_command=running_command,
        approval_policy=approval_policy,
        write_event=_write_json_event,
        agent=agent,
        runtime=runtime,
        trust_gate=trust_gate,
        configure_overrides=configure_overrides,
        coordinator=coordinator,
        queued_commands=queued_commands,
    )


def _handle_rpc_configure_command(
    command: dict[str, object],
    *,
    command_id: str,
    command_type: str,
    agent: CodingSession,
    runtime: WispRuntime,
    configure_overrides: _RpcConfigureOverrides | None = None,
) -> None:
    _rpc_execution.handle_rpc_configure_command(
        command,
        command_id=command_id,
        command_type=command_type,
        agent=agent,
        runtime=runtime,
        configure_overrides=configure_overrides,
        write_event=_write_json_event,
    )


def _auto_switch_provider_for_model(
    model: str,
    *,
    command_id: str,
    current_provider: Provider,
    runtime: WispRuntime,
    configure_overrides: _RpcConfigureOverrides | None,
) -> Provider:
    return _rpc_execution.auto_switch_provider_for_model(
        model,
        command_id=command_id,
        current_provider=current_provider,
        runtime=runtime,
        configure_overrides=configure_overrides,
        write_event=_write_json_event,
    )


def _handle_rpc_approval_command(
    command: dict[str, object],
    *,
    command_id: str,
    command_type: str,
    approval_policy: _RpcToolApprovalPolicy,
) -> None:
    _rpc_execution.handle_rpc_approval_command(
        command,
        command_id=command_id,
        command_type=command_type,
        approval_policy=approval_policy,
        write_event=_write_json_event,
    )


def _handle_rpc_trust_command(
    command: dict[str, object],
    *,
    command_id: str,
    command_type: str,
    trust_gate: _RpcTrustGate,
) -> None:
    _rpc_execution.handle_rpc_trust_command(
        command,
        command_id=command_id,
        command_type=command_type,
        trust_gate=trust_gate,
        write_event=_write_json_event,
    )


def _handle_rpc_cancel_command(
    command: dict[str, object],
    *,
    command_id: str,
    command_type: str,
    running_command: _RpcRunningCommand | None,
    coordinator: RpcCoordinator | None = None,
    queued_commands: deque[dict[str, object]] | None = None,
) -> None:
    _rpc_execution.handle_rpc_cancel_command(
        command,
        command_id=command_id,
        command_type=command_type,
        running_command=running_command,
        coordinator=coordinator,
        queued_commands=queued_commands,
        write_event=_write_json_event,
    )


def _write_rpc_command_error(*, command_id: str, command_type: str, message: str) -> None:
    _rpc_execution.write_rpc_command_error(
        command_id=command_id,
        command_type=command_type,
        message=message,
        write_event=_write_json_event,
    )


def _rpc_command_identity(command: dict[str, object]) -> tuple[str, str, str | None]:
    return _rpc_execution.rpc_command_identity(command)


def _rpc_command_type(command: dict[str, object]) -> str:
    return _rpc_execution.rpc_command_type(command)


def _rpc_command_id(command: dict[str, object]) -> tuple[str, str | None]:
    return _rpc_execution.rpc_command_id(command)


def _parse_rpc_command(line: str) -> dict[str, object] | None:
    return _rpc_stdin_transport().parse_command(line)
