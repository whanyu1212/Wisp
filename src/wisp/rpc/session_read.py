"""Read-only session pagination for the RPC frontend."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

import anyio
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream

from wisp.agent.messages import Message
from wisp.events import (
    RpcCommandFinished,
    RpcMessagesReported,
    RpcSessionsReported,
    RpcSessionSummary,
    RpcSessionTreeNode,
    RpcSessionTreeReported,
)
from wisp.rpc.commands import GetMessagesCommand, GetSessionsCommand, GetSessionTreeCommand
from wisp.rpc.coordinator import (
    _RpcCommandCompleted,
    _RpcControlEvent,
    _RpcRunningCommand,
    _RpcSessionState,
)
from wisp.rpc.lifecycle import RpcCommandLifecycle, RpcEventWriter
from wisp.rpc.session_state import rpc_selected_session_state, updated_rpc_session_state
from wisp.sessions.jsonl import (
    JsonlSession,
    JsonlSessionStore,
    SessionError,
    SessionMessagePage,
    SessionSummary,
    SessionTreeNodeSummary,
    SessionTreePage,
)

type RunningCommandFactory = Callable[..., _RpcRunningCommand]
type CommandCompletedFactory = Callable[..., _RpcCommandCompleted]


async def _run_abandonable_session_read[T](func: Callable[..., T], *args: object) -> T:
    return await anyio.to_thread.run_sync(func, *args, abandon_on_cancel=True)


def start_rpc_messages_command(
    command: GetMessagesCommand,
    *,
    provided_fields: frozenset[str],
    sessions: JsonlSessionStore,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> _RpcRunningCommand | None:
    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    command_id = lifecycle.command_id
    if "entry_ids" in provided_fields and command.entry_ids is None:
        lifecycle.fail("RPC get_messages command field entry_ids must contain non-empty strings")
        return None
    if (
        "complete_structure" in provided_fields
        and command.complete_structure is None
        or "full_content" in provided_fields
        and command.full_content is None
    ):
        lifecycle.fail(
            "RPC get_messages complete_structure and full_content fields must be booleans"
        )
        return None

    limit = command.limit
    session_id = command.session_id
    before_entry_id = command.before_entry_id
    after_entry_id = command.after_entry_id
    entry_ids = command.entry_ids or ()
    complete_structure = command.complete_structure is True
    full_content = command.full_content is True
    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_messages_command,
        sessions,
        session_state.session,
        session_state.entry_count,
        session_id,
        limit,
        before_entry_id,
        after_entry_id,
        entry_ids,
        complete_structure,
        full_content,
        command_id,
        cancel_scope,
        send.clone(),
        write_event,
        command_completed_factory,
    )
    return running_command_factory(
        command_id=command_id,
        command_type="get_messages",
        cancel_scope=cancel_scope,
    )


def start_rpc_sessions_command(
    command: GetSessionsCommand,
    *,
    sessions: JsonlSessionStore,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> _RpcRunningCommand:
    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    command_id = lifecycle.command_id
    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_sessions_command,
        sessions,
        session_state.session,
        session_state.entry_count,
        command.limit,
        command_id,
        cancel_scope,
        send.clone(),
        write_event,
        command_completed_factory,
    )
    return running_command_factory(
        command_id=command_id,
        command_type="get_sessions",
        cancel_scope=cancel_scope,
    )


def start_rpc_session_tree_command(
    command: GetSessionTreeCommand,
    *,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> _RpcRunningCommand:
    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    command_id = lifecycle.command_id
    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_session_tree_command,
        session_state.session,
        session_state.entry_count,
        command.limit,
        command.after_entry_id,
        command_id,
        cancel_scope,
        send.clone(),
        write_event,
        command_completed_factory,
    )
    return running_command_factory(
        command_id=command_id,
        command_type="get_session_tree",
        cancel_scope=cancel_scope,
    )


async def run_rpc_messages_command(
    sessions: JsonlSessionStore,
    selected_session: JsonlSession | None,
    selected_entry_count: int,
    session_id: str | None,
    limit: int,
    before_entry_id: str | None,
    after_entry_id: str | None,
    entry_ids: tuple[str, ...],
    complete_structure: bool,
    full_content: bool,
    command_id: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> None:
    ok = False
    error: str | None = None
    refreshed_history: tuple[Message, ...] | None = None
    refreshed_entry_count = selected_entry_count
    try:
        with cancel_scope:
            selected_read = session_id is None
            if session_id is None:
                session = selected_session
            else:
                session = await _run_abandonable_session_read(sessions.load, session_id)

            if session is None:
                page = SessionMessagePage(
                    session_id=None,
                    path=None,
                    active_leaf_id=None,
                    messages=(),
                    truncated=False,
                    next_before_entry_id=None,
                    next_after_entry_id=None,
                )
            elif not session.path.is_file():
                page = SessionMessagePage(
                    session_id=session.session_id,
                    path=session.path,
                    active_leaf_id=None,
                    messages=(),
                    truncated=False,
                    next_before_entry_id=None,
                    next_after_entry_id=None,
                )
            else:
                page = await _run_abandonable_session_read(
                    partial(
                        session.read_message_page,
                        limit=limit,
                        before_entry_id=before_entry_id,
                        after_entry_id=after_entry_id,
                        entry_ids=entry_ids,
                        complete_structure=complete_structure,
                        full_content=full_content,
                    )
                )

            if selected_read and session is not None:
                refreshed_entry_count, refreshed_history = await _run_abandonable_session_read(
                    updated_rpc_session_state,
                    session,
                    (),
                    selected_entry_count,
                )
            if cancel_scope.cancel_called:
                error = "RPC get_messages command cancelled"
                refreshed_history = None
                refreshed_entry_count = selected_entry_count
            else:
                write_event(
                    RpcMessagesReported(
                        command_id=command_id,
                        session_id=page.session_id,
                        session_path=page.path,
                        active_leaf_id=page.active_leaf_id,
                        messages=page.messages,
                        truncated=page.truncated,
                        next_before_entry_id=page.next_before_entry_id,
                        next_after_entry_id=page.next_after_entry_id,
                    )
                )
                ok = True
        if cancel_scope.cancel_called and error is None:
            error = "RPC get_messages command cancelled"
    except BaseException as exc:
        if isinstance(exc, anyio.get_cancelled_exc_class()):
            error = "RPC get_messages command cancelled"
        else:
            error = str(exc)
    finally:
        write_event(
            RpcCommandFinished(
                command_id=command_id,
                command_type="get_messages",
                ok=ok,
                error=error,
            )
        )
        await send.send(
            command_completed_factory(
                command_id=command_id,
                command_type="get_messages",
                ok=ok,
                history=refreshed_history,
                entry_count=refreshed_entry_count,
            )
        )
        await send.aclose()


async def run_rpc_sessions_command(
    sessions: JsonlSessionStore,
    selected_session: JsonlSession | None,
    selected_entry_count: int,
    limit: int,
    command_id: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> None:
    ok = False
    error: str | None = None
    try:
        with cancel_scope:
            summaries = await _run_abandonable_session_read(
                partial(sessions.summaries, limit=limit)
            )
            selected_session_name = (
                await _run_abandonable_session_read(selected_session.read_name)
                if selected_session is not None
                else None
            )
            if cancel_scope.cancel_called:
                error = "RPC get_sessions command cancelled"
            else:
                write_event(
                    RpcSessionsReported(
                        command_id=command_id,
                        sessions=tuple(_rpc_session_summary(summary) for summary in summaries),
                        selected_session_id=(
                            selected_session.session_id if selected_session is not None else None
                        ),
                        selected_session_path=(
                            selected_session.path if selected_session is not None else None
                        ),
                        selected_session_name=selected_session_name,
                    )
                )
                ok = True
        if cancel_scope.cancel_called and error is None:
            error = "RPC get_sessions command cancelled"
    except BaseException as exc:
        if isinstance(exc, anyio.get_cancelled_exc_class()):
            error = "RPC get_sessions command cancelled"
        else:
            error = str(exc)
    finally:
        write_event(
            RpcCommandFinished(
                command_id=command_id,
                command_type="get_sessions",
                ok=ok,
                error=error,
            )
        )
        await send.send(
            command_completed_factory(
                command_id=command_id,
                command_type="get_sessions",
                ok=ok,
                history=None,
                entry_count=selected_entry_count,
            )
        )
        await send.aclose()


async def run_rpc_session_tree_command(
    session: JsonlSession | None,
    selected_entry_count: int,
    limit: int,
    after_entry_id: str | None,
    command_id: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> None:
    ok = False
    error: str | None = None
    refreshed_history: tuple[Message, ...] | None = None
    refreshed_entry_count = selected_entry_count
    try:
        with cancel_scope:
            if session is None:
                if after_entry_id is not None:
                    raise SessionError(f"Session tree cursor not found: {after_entry_id}")
                page = SessionTreePage(
                    session_id=None,
                    path=None,
                    active_leaf_id=None,
                    total_node_count=0,
                    nodes=(),
                    truncated=False,
                    next_after_entry_id=None,
                )
            else:
                page = await _run_abandonable_session_read(
                    partial(
                        session.read_tree_page,
                        limit=limit,
                        after_entry_id=after_entry_id,
                    )
                )
                if session.path.is_file():
                    (
                        refreshed_entry_count,
                        refreshed_history,
                        _,
                        _name,
                    ) = await _run_abandonable_session_read(
                        rpc_selected_session_state,
                        session,
                    )
            if cancel_scope.cancel_called:
                error = "RPC get_session_tree command cancelled"
                refreshed_history = None
                refreshed_entry_count = selected_entry_count
            else:
                write_event(
                    RpcSessionTreeReported(
                        command_id=command_id,
                        session_id=page.session_id,
                        session_path=page.path,
                        active_leaf_id=page.active_leaf_id,
                        total_node_count=page.total_node_count,
                        nodes=tuple(_rpc_session_tree_node(node) for node in page.nodes),
                        truncated=page.truncated,
                        next_after_entry_id=page.next_after_entry_id,
                    )
                )
                ok = True
        if cancel_scope.cancel_called and error is None:
            error = "RPC get_session_tree command cancelled"
    except BaseException as exc:
        if isinstance(exc, anyio.get_cancelled_exc_class()):
            error = "RPC get_session_tree command cancelled"
        else:
            error = str(exc)
    finally:
        write_event(
            RpcCommandFinished(
                command_id=command_id,
                command_type="get_session_tree",
                ok=ok,
                error=error,
            )
        )
        await send.send(
            command_completed_factory(
                command_id=command_id,
                command_type="get_session_tree",
                ok=ok,
                history=refreshed_history,
                entry_count=refreshed_entry_count,
            )
        )
        await send.aclose()


def _rpc_session_summary(summary: SessionSummary) -> RpcSessionSummary:
    return RpcSessionSummary(
        session_id=summary.session_id,
        session_path=summary.path,
        updated_at=summary.updated_at,
        entry_count=summary.entry_count,
        active_leaf_id=summary.active_leaf_id,
        name=summary.name,
    )


def _rpc_session_tree_node(summary: SessionTreeNodeSummary) -> RpcSessionTreeNode:
    return RpcSessionTreeNode(
        entry_id=summary.entry_id,
        parent_id=summary.parent_id,
        operation_id=summary.operation_id,
        created_at=summary.created_at,
        kind=summary.kind,
        role=summary.role,
        preview=summary.preview,
        preview_truncated=summary.preview_truncated,
    )
