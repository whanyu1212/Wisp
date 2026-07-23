"""Transport-independent command execution for the RPC frontend."""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from typing import Protocol, cast
from uuid import uuid4

import anyio
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream

from wisp.agent.execution import ToolResultProcessingError
from wisp.agent.messages import Message
from wisp.coding import CodingSession
from wisp.events import (
    AgentStarted,
    CodingSessionState,
    ErrorEvent,
    ModelProviderAutoSwitched,
    QueueItemsRemoved,
    QueueKind,
    QueueMode,
    RpcCommandFinished,
    RpcCommandStarted,
    RpcStateReported,
    RpcStateSnapshot,
    SessionStatsReported,
    WispEvent,
)
from wisp.providers.base import Provider, ProviderError
from wisp.providers.catalog import AmbiguousModelError, UnknownModelError
from wisp.rpc.commands import QUEUE_RPC_COMMAND_TYPES, ApprovalScope
from wisp.runtime.api import WispRuntime
from wisp.runtime.registry import UnknownProviderError, UnknownToolError
from wisp.sessions.entries import MessageSessionEntry
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore, SessionError
from wisp.sessions.replay import resolve_session_tree

from .rpc_configuration import _RpcConfigureOverrides
from .rpc_coordinator import (
    RpcCoordinator,
    _RpcCancelResult,
    _RpcCommandCompleted,
    _RpcControlEvent,
    _RpcDispatchResult,
    _RpcPromptReady,
    _RpcRunningCommand,
    _RpcSessionState,
)
from .types import _JsonOutputModeError

type RpcEventWriter = Callable[[WispEvent], None]
type RpcEventRenderer = Callable[[AsyncIterator[WispEvent]], Awaitable[None]]
type RunningCommandFactory = Callable[..., _RpcRunningCommand]
type CommandCompletedFactory = Callable[..., _RpcCommandCompleted]


class RpcApprovalResolver(Protocol):
    def resolve_approval(
        self,
        *,
        call_id: str,
        approved: bool,
        reason: str | None = None,
        scope: ApprovalScope = "once",
    ) -> bool: ...


class RpcTrustResolver(Protocol):
    async def resolve(self) -> bool: ...

    def resolve_request(
        self,
        *,
        request_id: str,
        trusted: bool,
        reason: str | None = None,
        transient: bool = False,
    ) -> bool: ...


class RpcCommandExecutor:
    """Validate, launch, and report RPC commands independently from stdin."""

    def __init__(
        self,
        *,
        agent: CodingSession,
        runtime: WispRuntime,
        sessions: JsonlSessionStore,
        session_state: _RpcSessionState,
        task_group: TaskGroup,
        send: MemoryObjectSendStream[_RpcControlEvent],
        approval_policy: RpcApprovalResolver,
        trust_gate: RpcTrustResolver,
        configure_overrides: _RpcConfigureOverrides,
        coordinator: RpcCoordinator,
        write_event: RpcEventWriter,
        render_events: RpcEventRenderer,
        running_command_factory: RunningCommandFactory = _RpcRunningCommand,
        command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
    ) -> None:
        self.agent = agent
        self.runtime = runtime
        self.sessions = sessions
        self.session_state = session_state
        self.task_group = task_group
        self.send = send
        self.approval_policy = approval_policy
        self.trust_gate = trust_gate
        self.configure_overrides = configure_overrides
        self.coordinator = coordinator
        self.write_event = write_event
        self.render_events = render_events
        self.running_command_factory = running_command_factory
        self.command_completed_factory = command_completed_factory

    def dispatch(
        self,
        command: dict[str, object],
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        self.coordinator.running_command = running_command
        command_type = rpc_command_type(command)
        if command_type == "prompt":
            return self._dispatch_prompt(command)
        if command_type == "compact":
            return self._dispatch_compact(command)
        if command_type == "get_session_stats":
            return self._dispatch_session_stats(command)
        if command_type == "get_state":
            return self._dispatch_state(command, running_command)
        if command_type in QUEUE_RPC_COMMAND_TYPES:
            return self._dispatch_queue(command, running_command)
        return self._dispatch_control(command, running_command)

    def _dispatch_prompt(self, command: dict[str, object]) -> _RpcDispatchResult:
        new_running_command, new_session = start_rpc_prompt_command(
            command,
            agent=self.agent,
            sessions=self.sessions,
            session_state=self.session_state,
            task_group=self.task_group,
            send=self.send,
            trust_gate=self.trust_gate,
            write_event=self.write_event,
            render_events=self.render_events,
            running_command_factory=self.running_command_factory,
            command_completed_factory=self.command_completed_factory,
        )
        return _RpcDispatchResult(
            running_command=new_running_command,
            selected_session=new_session,
        )

    def _dispatch_compact(self, command: dict[str, object]) -> _RpcDispatchResult:
        return _RpcDispatchResult(
            running_command=start_rpc_compact_command(
                command,
                agent=self.agent,
                session_state=self.session_state,
                task_group=self.task_group,
                send=self.send,
                trust_gate=self.trust_gate,
                write_event=self.write_event,
                render_events=self.render_events,
                running_command_factory=self.running_command_factory,
                command_completed_factory=self.command_completed_factory,
            )
        )

    def _dispatch_session_stats(self, command: dict[str, object]) -> _RpcDispatchResult:
        return _RpcDispatchResult(
            running_command=start_rpc_session_stats_command(
                command,
                agent=self.agent,
                session_state=self.session_state,
                task_group=self.task_group,
                send=self.send,
                write_event=self.write_event,
                running_command_factory=self.running_command_factory,
                command_completed_factory=self.command_completed_factory,
            )
        )

    def _dispatch_queue(
        self,
        command: dict[str, object],
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        handle_rpc_queue_command(
            command,
            agent=self.agent,
            session=self.session_state.session,
            write_event=self.write_event,
        )
        return _RpcDispatchResult(running_command=running_command)

    def _dispatch_state(
        self,
        command: dict[str, object],
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        handle_rpc_state_command(
            command,
            agent=self.agent,
            session=self.session_state.session,
            running_command=running_command,
            pending_prompt_queue_commands=tuple(self.coordinator.pending_prompt_queue_commands),
            write_event=self.write_event,
        )
        return _RpcDispatchResult(running_command=running_command)

    def _dispatch_control(
        self,
        command: dict[str, object],
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        should_shutdown = handle_rpc_control_command(
            command,
            running_command=running_command,
            approval_policy=self.approval_policy,
            agent=self.agent,
            runtime=self.runtime,
            trust_gate=self.trust_gate,
            configure_overrides=self.configure_overrides,
            coordinator=self.coordinator,
            write_event=self.write_event,
        )
        return _RpcDispatchResult(
            running_command=running_command,
            should_shutdown=should_shutdown,
        )

    def reject(self, command: dict[str, object], message: str) -> None:
        reject_rpc_command(command, message=message, write_event=self.write_event)


def rpc_session_state(session: JsonlSession | None) -> _RpcSessionState:
    if session is None or not session.path.is_file():
        return _RpcSessionState(session=session, history=(), entry_count=0)
    return _RpcSessionState(
        session=session,
        history=session.read_context_messages(),
        entry_count=len(session.read_entries()),
    )


def start_rpc_prompt_command(
    command: dict[str, object],
    *,
    agent: CodingSession,
    sessions: JsonlSessionStore,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    trust_gate: RpcTrustResolver,
    write_event: RpcEventWriter,
    render_events: RpcEventRenderer,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> tuple[_RpcRunningCommand | None, JsonlSession | None]:
    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return None, session_state.session

    prompt = command.get("prompt")
    if not isinstance(prompt, str):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC prompt command requires string field: prompt",
            write_event=write_event,
        )
        return None, session_state.session

    selected_session = session_state.session or sessions.create()
    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_prompt_command,
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
        write_event,
        render_events,
        command_completed_factory,
    )
    return (
        running_command_factory(
            command_id=command_id,
            command_type="prompt",
            cancel_scope=cancel_scope,
        ),
        selected_session,
    )


def start_rpc_compact_command(
    command: dict[str, object],
    *,
    agent: CodingSession,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    trust_gate: RpcTrustResolver,
    write_event: RpcEventWriter,
    render_events: RpcEventRenderer,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> _RpcRunningCommand | None:
    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return None

    raw_instructions = command.get("instructions")
    if raw_instructions is not None and not isinstance(raw_instructions, str):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC compact command field instructions must be a string",
            write_event=write_event,
        )
        return None
    instructions = raw_instructions.strip() or None if isinstance(raw_instructions, str) else None

    session = session_state.session
    if session is None or not session.path.is_file():
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC compact command requires an existing persisted session",
            write_event=write_event,
        )
        return None

    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_compact_command,
        agent,
        session,
        session_state.history,
        session_state.entry_count,
        instructions,
        command_id,
        cancel_scope,
        send.clone(),
        trust_gate,
        write_event,
        render_events,
        command_completed_factory,
    )
    return running_command_factory(
        command_id=command_id,
        command_type="compact",
        cancel_scope=cancel_scope,
    )


def start_rpc_session_stats_command(
    command: dict[str, object],
    *,
    agent: CodingSession,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> _RpcRunningCommand | None:
    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return None

    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_session_stats_command,
        agent,
        session_state.session,
        session_state.entry_count,
        command_id,
        cancel_scope,
        send.clone(),
        write_event,
        command_completed_factory,
    )
    return running_command_factory(
        command_id=command_id,
        command_type="get_session_stats",
        cancel_scope=cancel_scope,
    )


async def run_rpc_session_stats_command(
    agent: CodingSession,
    session: JsonlSession | None,
    entry_count: int,
    command_id: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
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
                    updated_rpc_session_state,
                    session,
                    (),
                    entry_count,
                )
            write_event(SessionStatsReported(command_id=command_id, stats=stats))
            ok = True
        if cancel_scope.cancel_called:
            error = "RPC get_session_stats command cancelled"
    except BaseException as exc:
        if isinstance(exc, anyio.get_cancelled_exc_class()):
            error = "RPC get_session_stats command cancelled"
        else:
            error = str(exc)
    finally:
        write_event(
            RpcCommandFinished(
                command_id=command_id,
                command_type="get_session_stats",
                ok=ok,
                error=error,
            )
        )
        await send.send(
            command_completed_factory(
                command_id=command_id,
                command_type="get_session_stats",
                ok=ok,
                history=refreshed_history,
                entry_count=refreshed_entry_count,
            )
        )
        await send.aclose()


async def run_rpc_prompt_command(
    agent: CodingSession,
    session: JsonlSession,
    committed_history: tuple[Message, ...],
    entry_start: int,
    prompt: str,
    command_id: str,
    command_type: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    trust_gate: RpcTrustResolver,
    write_event: RpcEventWriter,
    render_events: RpcEventRenderer,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> None:
    error: str | None = None
    run_entry_start = entry_start
    run_active_leaf_id: str | None = None
    run_start_captured = False

    async def track_run_start(events: AsyncIterator[WispEvent]) -> AsyncIterator[WispEvent]:
        nonlocal run_active_leaf_id, run_entry_start, run_start_captured
        async for event in events:
            if isinstance(event, AgentStarted):
                with anyio.CancelScope(shield=True):
                    run_entry_start, run_active_leaf_id = await anyio.to_thread.run_sync(
                        rpc_session_run_start,
                        session,
                        entry_start,
                    )
                    run_start_captured = True
                yield event
                with anyio.CancelScope(shield=True):
                    await send.send(_RpcPromptReady(command_id=command_id))
                continue
            yield event

    try:
        with cancel_scope:
            try:
                agent.trusted = await trust_gate.resolve()
                await render_events(
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
                error = str(exc)
            except anyio.get_cancelled_exc_class():
                error = f"RPC command cancelled: {command_id}"
    finally:
        cancelled = error is not None and error.startswith("RPC command cancelled:")
        if cancelled:
            crossed_completion_boundary = await anyio.to_thread.run_sync(
                rpc_has_durable_completion,
                session,
                run_entry_start,
                command_id,
            )
            if not crossed_completion_boundary and run_start_captured:
                rolled_back = await session.restore_active_leaf_for_operation(
                    run_entry_start,
                    run_active_leaf_id,
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
            updated_rpc_session_state,
            session,
            committed_history,
            entry_start,
        )
        async with send:
            if cancelled:
                assert error is not None
                write_event(ErrorEvent(message=error))
            write_event(
                RpcCommandFinished(
                    command_id=command_id,
                    command_type=command_type,
                    ok=error is None,
                    error=error,
                )
            )
            await send.send(
                command_completed_factory(
                    command_id=command_id,
                    command_type="prompt",
                    ok=error is None,
                    history=updated_history,
                    entry_count=entry_count,
                )
            )


async def run_rpc_compact_command(
    agent: CodingSession,
    session: JsonlSession,
    committed_history: tuple[Message, ...],
    entry_start: int,
    instructions: str | None,
    command_id: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    trust_gate: RpcTrustResolver,
    write_event: RpcEventWriter,
    render_events: RpcEventRenderer,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
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
                await render_events(track_errors(agent.compact(session, instructions=instructions)))
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
                updated_rpc_session_state,
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
                write_event(ErrorEvent(message=error))
            write_event(
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="compact",
                    ok=error is None,
                    error=error,
                )
            )
            await send.send(
                command_completed_factory(
                    command_id=command_id,
                    command_type="compact",
                    ok=error is None,
                    history=updated_history,
                    entry_count=entry_count,
                )
            )


def updated_rpc_history(
    session: JsonlSession,
    committed_history: tuple[Message, ...],
    entry_start: int,
) -> tuple[Message, ...]:
    return updated_rpc_session_state(session, committed_history, entry_start)[1]


def rpc_session_entry_count(session: JsonlSession, fallback: int) -> int:
    if not session.path.is_file():
        return fallback
    return len(session.read_entries())


def rpc_session_run_start(
    session: JsonlSession,
    fallback: int,
) -> tuple[int, str | None]:
    """Snapshot the audit offset and active leaf before a prompt starts writing."""

    if not session.path.is_file():
        return fallback, None
    entries = session.read_entries()
    return len(entries), resolve_session_tree(entries).active_leaf_id


def rpc_has_durable_completion(
    session: JsonlSession,
    entry_start: int,
    operation_id: str,
) -> bool:
    if not session.path.is_file():
        return False
    for entry in session.read_entries()[entry_start:]:
        if entry.operation_id != operation_id:
            continue
        if not isinstance(entry, MessageSessionEntry):
            continue
        message = entry.message
        if message.role == "assistant" and message.finish_reason is not None:
            return True
        if message.role == "tool" and message.tool_call_id is not None:
            return True
    return False


def updated_rpc_session_state(
    session: JsonlSession,
    committed_history: tuple[Message, ...],
    entry_start: int,
) -> tuple[int, tuple[Message, ...]]:
    if not session.path.is_file():
        return entry_start, committed_history
    entries = session.read_entries()
    return len(entries), session.read_context_messages()


def reject_rpc_command(
    command: dict[str, object],
    *,
    message: str,
    write_event: RpcEventWriter,
) -> None:
    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    write_rpc_command_error(
        command_id=command_id,
        command_type=command_type,
        message=id_error or message,
        write_event=write_event,
    )


def handle_rpc_queue_command(
    command: dict[str, object],
    *,
    agent: CodingSession,
    session: JsonlSession | None,
    write_event: RpcEventWriter,
) -> None:
    """Execute one synchronous queue command through the shared session facade."""

    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return

    removed: QueueItemsRemoved | None = None
    try:
        if command_type == "get_queue_state":
            state = agent.queue_state(session)
        elif command_type in {"steer", "follow_up"}:
            content = command.get("content")
            if not isinstance(content, str):
                write_rpc_command_error(
                    command_id=command_id,
                    command_type=command_type,
                    message=f"RPC {command_type} command requires string field: content",
                    write_event=write_event,
                )
                return
            state = agent.steer(content) if command_type == "steer" else agent.follow_up(content)
        elif command_type == "set_queue_mode":
            kind = require_rpc_queue_kind(command, command_type=command_type)
            mode = require_rpc_queue_mode(command)
            state = agent.set_queue_mode(kind, mode)
        elif command_type == "pop_queue":
            kind = require_rpc_queue_kind(command, command_type=command_type)
            popped, state = agent.pop_queue(kind)
            removed = QueueItemsRemoved(
                command_id=command_id,
                operation="pop",
                kind=kind,
                steering=(popped.content,) if popped is not None and kind == "steering" else (),
                follow_up=(popped.content,) if popped is not None and kind == "follow_up" else (),
            )
        elif command_type == "clear_queue":
            clear_kind = optional_rpc_queue_kind(command, command_type=command_type)
            cleared, state = agent.clear_queue(clear_kind)
            removed = QueueItemsRemoved(
                command_id=command_id,
                operation="clear",
                kind=clear_kind,
                steering=tuple(message.content for message in cleared.steering),
                follow_up=tuple(message.content for message in cleared.follow_up),
            )
        else:  # pragma: no cover - dispatch owns the closed command set
            raise AssertionError(f"Unsupported queue RPC command: {command_type}")
    except (RuntimeError, ValueError) as exc:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=str(exc),
            write_event=write_event,
        )
        return

    if removed is not None:
        write_event(removed)
    write_event(state)
    write_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


def handle_rpc_state_command(
    command: dict[str, object],
    *,
    agent: CodingSession,
    session: JsonlSession | None,
    running_command: _RpcRunningCommand | None,
    pending_prompt_queue_commands: tuple[dict[str, object], ...] = (),
    write_event: RpcEventWriter,
) -> None:
    """Return one coherent in-memory state snapshot without becoming active."""

    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return

    try:
        core_state = _project_buffered_prompt_queue_commands(
            agent.state_snapshot(session),
            pending_prompt_queue_commands,
        )
        state = RpcStateSnapshot(
            **core_state.model_dump(),
            session_id=session.session_id if session is not None else None,
            session_path=session.path if session is not None else None,
            active_command_id=(running_command.command_id if running_command is not None else None),
            active_command_type=(
                running_command.command_type if running_command is not None else None
            ),
            cancel_requested=(
                running_command.cancel_scope.cancel_called if running_command is not None else False
            ),
        )
    except Exception as exc:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=str(exc),
            write_event=write_event,
        )
        return

    write_event(RpcStateReported(command_id=command_id, state=state))
    write_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


def _project_buffered_prompt_queue_commands(
    state: CodingSessionState,
    commands: tuple[dict[str, object], ...],
) -> CodingSessionState:
    """Project prompt-startup queue commands without mutating coordinator/session state."""

    if not commands:
        return state
    steering_mode = state.steering_mode
    follow_up_mode = state.follow_up_mode
    steering_count = state.pending_steering_count
    follow_up_count = state.pending_follow_up_count
    for command in commands:
        command_type = rpc_command_type(command)
        _, id_error = rpc_command_id(command)
        if id_error is not None:
            continue
        try:
            if command_type == "steer":
                if isinstance(command.get("content"), str):
                    steering_count += 1
            elif command_type == "follow_up":
                if isinstance(command.get("content"), str):
                    follow_up_count += 1
            elif command_type == "set_queue_mode":
                kind = require_rpc_queue_kind(command, command_type=command_type)
                mode = require_rpc_queue_mode(command)
                if kind == "steering":
                    steering_mode = mode
                else:
                    follow_up_mode = mode
            elif command_type == "pop_queue":
                kind = require_rpc_queue_kind(command, command_type=command_type)
                if kind == "steering":
                    steering_count = max(0, steering_count - 1)
                else:
                    follow_up_count = max(0, follow_up_count - 1)
            elif command_type == "clear_queue":
                clear_kind = optional_rpc_queue_kind(command, command_type=command_type)
                if clear_kind is None:
                    steering_count = 0
                    follow_up_count = 0
                elif clear_kind == "steering":
                    steering_count = 0
                else:
                    follow_up_count = 0
        except ValueError:
            continue

    return state.model_copy(
        update={
            "steering_mode": steering_mode,
            "follow_up_mode": follow_up_mode,
            "pending_steering_count": steering_count,
            "pending_follow_up_count": follow_up_count,
        }
    )


def require_rpc_queue_kind(
    command: dict[str, object],
    *,
    command_type: str,
) -> QueueKind:
    kind = optional_rpc_queue_kind(command, command_type=command_type)
    if kind is None:
        raise ValueError(f"RPC {command_type} command field kind must be 'steering' or 'follow_up'")
    return kind


def optional_rpc_queue_kind(
    command: dict[str, object],
    *,
    command_type: str,
) -> QueueKind | None:
    kind = command.get("kind")
    if kind is None:
        return None
    if isinstance(kind, str) and kind in {"steering", "follow_up"}:
        return cast(QueueKind, kind)
    raise ValueError(f"RPC {command_type} command field kind must be 'steering' or 'follow_up'")


def require_rpc_queue_mode(command: dict[str, object]) -> QueueMode:
    mode = command.get("mode")
    if isinstance(mode, str) and mode in {"one_at_a_time", "all"}:
        return cast(QueueMode, mode)
    raise ValueError("RPC set_queue_mode command field mode must be 'one_at_a_time' or 'all'")


def handle_rpc_control_command(
    command: dict[str, object],
    *,
    running_command: _RpcRunningCommand | None,
    approval_policy: RpcApprovalResolver,
    write_event: RpcEventWriter,
    agent: CodingSession | None = None,
    runtime: WispRuntime | None = None,
    trust_gate: RpcTrustResolver | None = None,
    configure_overrides: _RpcConfigureOverrides | None = None,
    coordinator: RpcCoordinator | None = None,
    queued_commands: deque[dict[str, object]] | None = None,
) -> bool:
    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return False
    if command_type == "shutdown":
        write_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))
        return True
    if command_type == "cancel":
        handle_rpc_cancel_command(
            command,
            command_id=command_id,
            command_type=command_type,
            running_command=running_command,
            coordinator=coordinator,
            queued_commands=queued_commands,
            write_event=write_event,
        )
        return False
    if command_type == "approval":
        handle_rpc_approval_command(
            command,
            command_id=command_id,
            command_type=command_type,
            approval_policy=approval_policy,
            write_event=write_event,
        )
        return False
    if command_type == "trust":
        if trust_gate is None:
            write_rpc_command_error(
                command_id=command_id,
                command_type=command_type,
                message="RPC trust command requires an active trust gate",
                write_event=write_event,
            )
            return False
        handle_rpc_trust_command(
            command,
            command_id=command_id,
            command_type=command_type,
            trust_gate=trust_gate,
            write_event=write_event,
        )
        return False
    if command_type == "configure":
        if agent is None or runtime is None:
            write_rpc_command_error(
                command_id=command_id,
                command_type=command_type,
                message="RPC configure command requires an active agent runtime",
                write_event=write_event,
            )
            return False
        handle_rpc_configure_command(
            command,
            command_id=command_id,
            command_type=command_type,
            agent=agent,
            runtime=runtime,
            configure_overrides=configure_overrides,
            write_event=write_event,
        )
        return False
    message = f"Unknown RPC command: {command_type}"
    write_rpc_command_error(
        command_id=command_id,
        command_type=command_type,
        message=message,
        write_event=write_event,
    )
    return False


def handle_rpc_configure_command(
    command: dict[str, object],
    *,
    command_id: str,
    command_type: str,
    agent: CodingSession,
    runtime: WispRuntime,
    write_event: RpcEventWriter,
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
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC configure command requires provider, model, or effort",
            write_event=write_event,
        )
        return
    if provider is not None and not isinstance(provider, str):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC configure command field provider must be a string",
            write_event=write_event,
        )
        return
    if model is not None and not isinstance(model, str):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC configure command field model must be a string",
            write_event=write_event,
        )
        return
    if effort is not None and not isinstance(effort, str):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC configure command field effort must be a string",
            write_event=write_event,
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
            write_rpc_command_error(
                command_id=command_id,
                command_type=command_type,
                message=str(exc),
                write_event=write_event,
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
            selected_effort = None
            if configure_overrides is not None:
                configure_overrides.effort = None
                configure_overrides.has_effort = True
    if has_model and provider is None and isinstance(model, str):
        selected_provider = auto_switch_provider_for_model(
            model,
            command_id=command_id,
            current_provider=selected_provider,
            runtime=runtime,
            configure_overrides=configure_overrides,
            write_event=write_event,
        )
        if not has_effort:
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
    write_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


def auto_switch_provider_for_model(
    model: str,
    *,
    command_id: str,
    current_provider: Provider,
    runtime: WispRuntime,
    configure_overrides: _RpcConfigureOverrides | None,
    write_event: RpcEventWriter,
) -> Provider:
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
    write_event(
        ModelProviderAutoSwitched(command_id=command_id, provider=resolved_provider, model=model)
    )
    if configure_overrides is not None:
        configure_overrides.provider = resolved_provider
    return new_provider


def handle_rpc_approval_command(
    command: dict[str, object],
    *,
    command_id: str,
    command_type: str,
    approval_policy: RpcApprovalResolver,
    write_event: RpcEventWriter,
) -> None:
    call_id = command.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC approval command requires string field: call_id",
            write_event=write_event,
        )
        return
    approved = command.get("approved")
    if not isinstance(approved, bool):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC approval command requires boolean field: approved",
            write_event=write_event,
        )
        return
    reason = command.get("reason")
    if reason is not None and not isinstance(reason, str):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC approval command field reason must be a string",
            write_event=write_event,
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
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=(
                "RPC approval command field scope must be one of: once, tool_session, all_session"
            ),
            write_event=write_event,
        )
        return
    if not approved and scope != "once":
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC approval scope is only valid for approved requests",
            write_event=write_event,
        )
        return
    if not approval_policy.resolve_approval(
        call_id=call_id,
        approved=approved,
        reason=reason,
        scope=scope,
    ):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=f"No pending tool approval with call_id: {call_id}",
            write_event=write_event,
        )
        return
    write_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


def handle_rpc_trust_command(
    command: dict[str, object],
    *,
    command_id: str,
    command_type: str,
    trust_gate: RpcTrustResolver,
    write_event: RpcEventWriter,
) -> None:
    request_id = command.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC trust command requires string field: request_id",
            write_event=write_event,
        )
        return
    trusted = command.get("trusted")
    if not isinstance(trusted, bool):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC trust command requires boolean field: trusted",
            write_event=write_event,
        )
        return
    reason = command.get("reason")
    if reason is not None and not isinstance(reason, str):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC trust command field reason must be a string",
            write_event=write_event,
        )
        return
    transient = command.get("transient")
    if transient is not None and not isinstance(transient, bool):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC trust command field transient must be a boolean",
            write_event=write_event,
        )
        return
    if not trust_gate.resolve_request(
        request_id=request_id,
        trusted=trusted,
        reason=reason,
        transient=transient is True,
    ):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=f"No pending trust request with request_id: {request_id}",
            write_event=write_event,
        )
        return
    write_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


def handle_rpc_cancel_command(
    command: dict[str, object],
    *,
    command_id: str,
    command_type: str,
    running_command: _RpcRunningCommand | None,
    write_event: RpcEventWriter,
    coordinator: RpcCoordinator | None = None,
    queued_commands: deque[dict[str, object]] | None = None,
) -> None:
    target_id = command.get("target_id")
    if not isinstance(target_id, str) or not target_id:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC cancel command requires string field: target_id",
            write_event=write_event,
        )
        return
    if coordinator is not None:
        result = coordinator.cancel(target_id)
    else:
        result = legacy_rpc_cancel(
            target_id,
            running_command=running_command,
            queued_commands=queued_commands,
        )
    if result.outcome == "running":
        write_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))
        return
    queued_target = result.command
    if queued_target is None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=f"No running or queued RPC command with id: {target_id}",
            write_event=write_event,
        )
        return
    target_type = rpc_command_type(queued_target)
    write_event(RpcCommandStarted(command_id=target_id, command_type=target_type))
    write_event(
        RpcCommandFinished(
            command_id=target_id,
            command_type=target_type,
            ok=False,
            error=f"RPC command cancelled: {target_id}",
        )
    )
    write_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


def legacy_rpc_cancel(
    target_id: str,
    *,
    running_command: _RpcRunningCommand | None,
    queued_commands: deque[dict[str, object]] | None,
) -> _RpcCancelResult:
    if running_command is not None and running_command.command_id == target_id:
        running_command.cancel_scope.cancel()
        return _RpcCancelResult("running")
    queued_target = next(
        (queued for queued in queued_commands or () if queued.get("id") == target_id),
        None,
    )
    if queued_target is None:
        return _RpcCancelResult("missing")
    assert queued_commands is not None
    queued_commands.remove(queued_target)
    return _RpcCancelResult("queued", command=queued_target)


def write_rpc_command_error(
    *,
    command_id: str,
    command_type: str,
    message: str,
    write_event: RpcEventWriter,
) -> None:
    write_event(ErrorEvent(message=message))
    write_event(
        RpcCommandFinished(
            command_id=command_id,
            command_type=command_type,
            ok=False,
            error=message,
        )
    )


def rpc_command_identity(command: dict[str, object]) -> tuple[str, str, str | None]:
    command_type = rpc_command_type(command)
    command_id, id_error = rpc_command_id(command)
    return command_type, command_id, id_error


def rpc_command_type(command: dict[str, object]) -> str:
    command_type = command.get("type")
    return command_type if isinstance(command_type, str) and command_type else "unknown"


def rpc_command_id(command: dict[str, object]) -> tuple[str, str | None]:
    command_id = command.get("id")
    if command_id is None:
        return uuid4().hex, None
    if isinstance(command_id, str) and command_id:
        return command_id, None
    return uuid4().hex, "RPC command id must be a non-empty string"


__all__ = ["RpcCommandExecutor"]
