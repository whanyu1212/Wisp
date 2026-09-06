"""Persisted-session mutation and navigation for the RPC frontend."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import anyio
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream

from wisp.agent.messages import Message
from wisp.events import (
    RpcCommandFinished,
    RpcSessionCloned,
    RpcSessionForked,
    RpcSessionNameChanged,
    RpcSessionSelected,
    RpcSessionTreeNavigated,
    RpcSessionTreeUnreverted,
    WispEvent,
)
from wisp.rpc.commands import (
    CloneSessionCommand,
    ForkSessionCommand,
    NavigateSessionTreeCommand,
    SelectSessionCommand,
    SetSessionNameCommand,
    UnrevertSessionTreeCommand,
)
from wisp.rpc.coordinator import (
    _RpcCommandCompleted,
    _RpcControlEvent,
    _RpcRunningCommand,
    _RpcSessionState,
)
from wisp.rpc.lifecycle import RpcCommandLifecycle, RpcEventWriter
from wisp.rpc.session_state import rpc_derived_session_state, rpc_selected_session_state
from wisp.sessions.errors import SessionNavigationCancelledError
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore, SessionError

type RunningCommandFactory = Callable[..., _RpcRunningCommand]
type CommandCompletedFactory = Callable[..., _RpcCommandCompleted]


async def _run_abandonable_session_read[T](func: Callable[..., T], *args: object) -> T:
    return await anyio.to_thread.run_sync(func, *args, abandon_on_cancel=True)


def _normalized_session_path(session: JsonlSession) -> Path:
    return session.path.expanduser().resolve(strict=False)


def start_rpc_select_session_command(
    command: SelectSessionCommand,
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
    session_id = command.session_id
    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_select_session_command,
        sessions,
        session_state.entry_count,
        session_id,
        command_id,
        cancel_scope,
        send.clone(),
        write_event,
        command_completed_factory,
    )
    return running_command_factory(
        command_id=command_id,
        command_type="select_session",
        cancel_scope=cancel_scope,
    )


def start_rpc_clone_session_command(
    command: CloneSessionCommand,
    *,
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
    if session_state.session is None:
        lifecycle.fail("RPC clone_session command requires a selected session")
        return None

    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_clone_session_command,
        sessions,
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
        command_type="clone_session",
        cancel_scope=cancel_scope,
    )


def start_rpc_fork_session_command(
    command: ForkSessionCommand,
    *,
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
    entry_id = command.entry_id
    if session_state.session is None:
        lifecycle.fail("RPC fork_session command requires a selected session")
        return None

    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_fork_session_command,
        sessions,
        session_state.session,
        session_state.entry_count,
        entry_id,
        command_id,
        cancel_scope,
        send.clone(),
        write_event,
        command_completed_factory,
    )
    return running_command_factory(
        command_id=command_id,
        command_type="fork_session",
        cancel_scope=cancel_scope,
    )


def start_rpc_navigate_session_tree_command(
    command: NavigateSessionTreeCommand,
    *,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> _RpcRunningCommand | None:
    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    command_id = lifecycle.command_id
    entry_id = command.entry_id
    session = session_state.session
    if session is None or not session.path.is_file():
        lifecycle.fail("RPC navigate_session_tree command requires an existing persisted session")
        return None

    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_navigate_session_tree_command,
        session,
        session_state.entry_count,
        entry_id,
        command_id,
        cancel_scope,
        send.clone(),
        write_event,
        command_completed_factory,
    )
    return running_command_factory(
        command_id=command_id,
        command_type="navigate_session_tree",
        cancel_scope=cancel_scope,
    )


def start_rpc_unrevert_session_tree_command(
    command: UnrevertSessionTreeCommand,
    *,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> _RpcRunningCommand | None:
    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    command_id = lifecycle.command_id
    session = session_state.session
    if session is None or not session.path.is_file():
        lifecycle.fail("RPC unrevert_session_tree command requires an existing persisted session")
        return None

    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_unrevert_session_tree_command,
        session,
        session_state.entry_count,
        command_id,
        cancel_scope,
        send.clone(),
        write_event,
        command_completed_factory,
    )
    return running_command_factory(
        command_id=command_id,
        command_type="unrevert_session_tree",
        cancel_scope=cancel_scope,
    )


def start_rpc_set_session_name_command(
    command: SetSessionNameCommand,
    *,
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
    name = command.name
    session_id = command.session_id
    selected_session = session_state.session
    if session_id is None and selected_session is None:
        lifecycle.fail("RPC set_session_name command requires a selected session or session_id")
        return None

    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_set_session_name_command,
        sessions,
        selected_session,
        session_state.entry_count,
        session_id,
        name,
        command_id,
        cancel_scope,
        send.clone(),
        write_event,
        command_completed_factory,
    )
    return running_command_factory(
        command_id=command_id,
        command_type="set_session_name",
        cancel_scope=cancel_scope,
    )


async def run_rpc_select_session_command(
    sessions: JsonlSessionStore,
    selected_entry_count: int,
    session_id: str,
    command_id: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> None:
    ok = False
    error: str | None = None
    selected_session: JsonlSession | None = None
    refreshed_history: tuple[Message, ...] | None = None
    refreshed_entry_count = selected_entry_count
    active_leaf_id: str | None = None
    refreshed_name: str | None = None
    post_apply_events: tuple[WispEvent, ...] = ()
    finish_in_worker = True
    try:
        with cancel_scope:
            loaded_session = await _run_abandonable_session_read(sessions.load, session_id)
            (
                refreshed_entry_count,
                refreshed_history,
                active_leaf_id,
                refreshed_name,
            ) = await _run_abandonable_session_read(rpc_selected_session_state, loaded_session)
            if cancel_scope.cancel_called:
                error = "RPC select_session command cancelled"
                refreshed_history = None
                refreshed_entry_count = selected_entry_count
            else:
                selected = RpcSessionSelected(
                    command_id=command_id,
                    session_id=loaded_session.session_id,
                    session_path=loaded_session.path,
                    active_leaf_id=active_leaf_id,
                    entry_count=refreshed_entry_count,
                    session_name=refreshed_name,
                )
                selected_session = loaded_session
                post_apply_events = (
                    selected,
                    RpcCommandFinished(
                        command_id=command_id,
                        command_type="select_session",
                        ok=True,
                    ),
                )
                finish_in_worker = False
                ok = True
        if cancel_scope.cancel_called and error is None:
            error = "RPC select_session command cancelled"
    except BaseException as exc:
        selected_session = None
        refreshed_history = None
        refreshed_entry_count = selected_entry_count
        post_apply_events = ()
        finish_in_worker = True
        if isinstance(exc, anyio.get_cancelled_exc_class()):
            error = "RPC select_session command cancelled"
        else:
            error = str(exc)
    finally:
        if finish_in_worker:
            write_event(
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="select_session",
                    ok=ok,
                    error=error,
                )
            )
        await send.send(
            command_completed_factory(
                command_id=command_id,
                command_type="select_session",
                ok=ok,
                history=refreshed_history,
                entry_count=refreshed_entry_count,
                selected_session=selected_session,
                session_name=refreshed_name,
                session_name_updated=ok,
                post_apply_events=post_apply_events,
            )
        )
        await send.aclose()


async def run_rpc_clone_session_command(
    sessions: JsonlSessionStore,
    source_session: JsonlSession,
    selected_entry_count: int,
    command_id: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> None:
    ok = False
    error: str | None = None
    selected_session: JsonlSession | None = None
    refreshed_history: tuple[Message, ...] | None = None
    refreshed_entry_count = selected_entry_count
    refreshed_name: str | None = None
    post_apply_events: tuple[WispEvent, ...] = ()
    finish_in_worker = True
    try:
        with cancel_scope:
            await anyio.sleep(0)
            if not source_session.path.is_file():
                raise SessionError("Cannot clone an empty session")
            source_active_leaf_id = await _run_abandonable_session_read(
                source_session.read_active_leaf_id
            )
            await anyio.sleep(0)
            # Publishing a derived session is the durable commit boundary. Once
            # entered, cancellation must not leave a created target reported as
            # failed or silently detached from the coordinator.
            with anyio.CancelScope(shield=True):
                cloned_session = await sessions.clone(
                    source_session,
                    expected_active_leaf_id=source_active_leaf_id,
                )
                (
                    refreshed_entry_count,
                    refreshed_history,
                    active_leaf_id,
                    refreshed_name,
                ) = await anyio.to_thread.run_sync(
                    rpc_selected_session_state,
                    cloned_session,
                )
            cloned = RpcSessionCloned(
                command_id=command_id,
                source_session_id=source_session.session_id,
                source_session_path=source_session.path,
                source_active_leaf_id=source_active_leaf_id,
                source_session_name=refreshed_name,
                session_id=cloned_session.session_id,
                session_path=cloned_session.path,
                active_leaf_id=active_leaf_id,
                session_name=refreshed_name,
                entry_count=refreshed_entry_count,
            )
            selected_session = cloned_session
            post_apply_events = (
                cloned,
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="clone_session",
                    ok=True,
                ),
            )
            finish_in_worker = False
            ok = True
        if cancel_scope.cancel_called and not ok:
            error = "RPC clone_session command cancelled"
    except BaseException as exc:
        selected_session = None
        refreshed_history = None
        refreshed_entry_count = selected_entry_count
        post_apply_events = ()
        finish_in_worker = True
        if isinstance(exc, anyio.get_cancelled_exc_class()):
            error = "RPC clone_session command cancelled"
        else:
            error = str(exc)
    finally:
        if finish_in_worker:
            write_event(
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="clone_session",
                    ok=ok,
                    error=error,
                )
            )
        await send.send(
            command_completed_factory(
                command_id=command_id,
                command_type="clone_session",
                ok=ok,
                history=refreshed_history,
                entry_count=refreshed_entry_count,
                selected_session=selected_session,
                session_name=refreshed_name,
                session_name_updated=ok,
                post_apply_events=post_apply_events,
            )
        )
        await send.aclose()


async def run_rpc_fork_session_command(
    sessions: JsonlSessionStore,
    source_session: JsonlSession,
    selected_entry_count: int,
    entry_id: str,
    command_id: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> None:
    ok = False
    error: str | None = None
    selected_session: JsonlSession | None = None
    refreshed_history: tuple[Message, ...] | None = None
    refreshed_entry_count = selected_entry_count
    refreshed_name: str | None = None
    post_apply_events: tuple[WispEvent, ...] = ()
    finish_in_worker = True
    try:
        with cancel_scope:
            await anyio.sleep(0)
            if not source_session.path.is_file():
                raise SessionError("Cannot fork an empty session")
            source_active_leaf_id = await _run_abandonable_session_read(
                source_session.read_active_leaf_id
            )
            await anyio.sleep(0)
            with anyio.CancelScope(shield=True):
                fork_result = await sessions.fork_from_user_message(
                    source_session,
                    entry_id,
                    expected_active_leaf_id=source_active_leaf_id,
                )
                (
                    refreshed_entry_count,
                    refreshed_history,
                    active_leaf_id,
                    refreshed_name,
                ) = await anyio.to_thread.run_sync(
                    rpc_derived_session_state,
                    fork_result.session,
                )
            forked = RpcSessionForked(
                command_id=command_id,
                source_session_id=fork_result.source_session_id,
                source_session_path=source_session.path,
                source_active_leaf_id=fork_result.source_active_leaf_id,
                source_session_name=fork_result.source_session_name,
                session_id=fork_result.session.session_id,
                session_path=fork_result.session.path,
                active_leaf_id=active_leaf_id,
                session_name=refreshed_name,
                entry_count=refreshed_entry_count,
                selected_entry_id=fork_result.selected_entry_id,
                selected_prompt=fork_result.selected_prompt,
            )
            selected_session = fork_result.session
            post_apply_events = (
                forked,
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="fork_session",
                    ok=True,
                ),
            )
            finish_in_worker = False
            ok = True
        if cancel_scope.cancel_called and not ok:
            error = "RPC fork_session command cancelled"
    except BaseException as exc:
        selected_session = None
        refreshed_history = None
        refreshed_entry_count = selected_entry_count
        post_apply_events = ()
        finish_in_worker = True
        if isinstance(exc, anyio.get_cancelled_exc_class()):
            error = "RPC fork_session command cancelled"
        else:
            error = str(exc)
    finally:
        if finish_in_worker:
            write_event(
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="fork_session",
                    ok=ok,
                    error=error,
                )
            )
        await send.send(
            command_completed_factory(
                command_id=command_id,
                command_type="fork_session",
                ok=ok,
                history=refreshed_history,
                entry_count=refreshed_entry_count,
                selected_session=selected_session,
                session_name=refreshed_name,
                session_name_updated=ok,
                post_apply_events=post_apply_events,
            )
        )
        await send.aclose()


async def run_rpc_navigate_session_tree_command(
    session: JsonlSession,
    selected_entry_count: int,
    entry_id: str,
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
    post_apply_events: tuple[WispEvent, ...] = ()
    finish_in_worker = True
    try:
        with cancel_scope:
            await anyio.sleep(0)
            expected_active_leaf_id = await _run_abandonable_session_read(
                session.read_active_leaf_id
            )
            await anyio.sleep(0)
            navigation = await session.navigate_tree(
                entry_id,
                expected_active_leaf_id=expected_active_leaf_id,
                operation_id=command_id,
                cancel_requested=lambda: cancel_scope.cancel_called,
            )
            # A changed navigation has crossed its durable commit boundary.
            # Replay and coordinator publication must then complete even if
            # cancellation arrives after the active-leaf append.
            with anyio.CancelScope(shield=navigation.changed):
                (
                    refreshed_entry_count,
                    refreshed_history,
                    active_leaf_id,
                    _name,
                ) = await anyio.to_thread.run_sync(
                    rpc_selected_session_state,
                    session,
                )
            if cancel_scope.cancel_called and not navigation.changed:
                refreshed_history = None
                refreshed_entry_count = selected_entry_count
                error = "RPC navigate_session_tree command cancelled"
                return
            navigated = RpcSessionTreeNavigated(
                command_id=command_id,
                session_id=session.session_id,
                session_path=session.path,
                selected_entry_id=navigation.selected_entry_id,
                previous_active_leaf_id=navigation.previous_active_leaf_id,
                active_leaf_id=active_leaf_id,
                editor_text=navigation.editor_text,
                changed=navigation.changed,
                entry_count=refreshed_entry_count,
            )
            post_apply_events = (
                navigated,
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="navigate_session_tree",
                    ok=True,
                ),
            )
            finish_in_worker = False
            ok = True
        if cancel_scope.cancel_called and not ok:
            error = "RPC navigate_session_tree command cancelled"
    except BaseException as exc:
        refreshed_history = None
        refreshed_entry_count = selected_entry_count
        post_apply_events = ()
        finish_in_worker = True
        if isinstance(
            exc,
            (anyio.get_cancelled_exc_class(), SessionNavigationCancelledError),
        ):
            error = "RPC navigate_session_tree command cancelled"
        else:
            error = str(exc)
    finally:
        if finish_in_worker:
            write_event(
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="navigate_session_tree",
                    ok=ok,
                    error=error,
                )
            )
        await send.send(
            command_completed_factory(
                command_id=command_id,
                command_type="navigate_session_tree",
                ok=ok,
                history=refreshed_history,
                entry_count=refreshed_entry_count,
                post_apply_events=post_apply_events,
            )
        )
        await send.aclose()


async def run_rpc_unrevert_session_tree_command(
    session: JsonlSession,
    selected_entry_count: int,
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
    post_apply_events: tuple[WispEvent, ...] = ()
    finish_in_worker = True
    try:
        with cancel_scope:
            await anyio.sleep(0)
            expected_active_leaf_id = await _run_abandonable_session_read(
                session.read_active_leaf_id
            )
            await anyio.sleep(0)
            unrevert = await session.unrevert_tree(
                expected_active_leaf_id=expected_active_leaf_id,
                operation_id=command_id,
                cancel_requested=lambda: cancel_scope.cancel_called,
            )
            # The active-leaf append is the durable commit boundary. Publish
            # refreshed coordinator state even if cancellation arrives afterward.
            with anyio.CancelScope(shield=True):
                (
                    refreshed_entry_count,
                    refreshed_history,
                    _active_leaf_id,
                    _name,
                ) = await anyio.to_thread.run_sync(rpc_selected_session_state, session)
            event = RpcSessionTreeUnreverted(
                command_id=command_id,
                session_id=session.session_id,
                session_path=session.path,
                source_transition_id=unrevert.source_transition_id,
                previous_active_leaf_id=unrevert.previous_active_leaf_id,
                active_leaf_id=unrevert.active_leaf_id,
                entry_count=refreshed_entry_count,
            )
            post_apply_events = (
                event,
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="unrevert_session_tree",
                    ok=True,
                ),
            )
            finish_in_worker = False
            ok = True
        if cancel_scope.cancel_called and not ok:
            error = "RPC unrevert_session_tree command cancelled"
    except BaseException as exc:
        refreshed_history = None
        refreshed_entry_count = selected_entry_count
        post_apply_events = ()
        finish_in_worker = True
        if isinstance(
            exc,
            (anyio.get_cancelled_exc_class(), SessionNavigationCancelledError),
        ):
            error = "RPC unrevert_session_tree command cancelled"
        else:
            error = str(exc)
    finally:
        if finish_in_worker:
            write_event(
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="unrevert_session_tree",
                    ok=ok,
                    error=error,
                )
            )
        await send.send(
            command_completed_factory(
                command_id=command_id,
                command_type="unrevert_session_tree",
                ok=ok,
                history=refreshed_history,
                entry_count=refreshed_entry_count,
                post_apply_events=post_apply_events,
            )
        )
        await send.aclose()


async def run_rpc_set_session_name_command(
    sessions: JsonlSessionStore,
    selected_session: JsonlSession | None,
    selected_entry_count: int,
    session_id: str | None,
    name: str,
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
    refreshed_name: str | None = None
    selected_target = False
    post_apply_events: tuple[WispEvent, ...] = ()
    finish_in_worker = True
    try:
        with cancel_scope:
            await anyio.sleep(0)
            if session_id is None:
                if selected_session is None:
                    raise SessionError(
                        "RPC set_session_name command requires a selected session or session_id"
                    )
                target = selected_session
                selected_target = True
            else:
                target = await _run_abandonable_session_read(sessions.load, session_id)
                selected_target = (
                    selected_session is not None
                    and target.session_id == selected_session.session_id
                    and _normalized_session_path(target)
                    == _normalized_session_path(selected_session)
                )
            await anyio.sleep(0)
            with anyio.CancelScope(shield=True):
                change = await target.set_name(
                    name,
                    operation_id=command_id,
                    cancel_requested=lambda: cancel_scope.cancel_called,
                )
                if selected_target:
                    (
                        refreshed_entry_count,
                        refreshed_history,
                        _active_leaf_id,
                        refreshed_name,
                    ) = await anyio.to_thread.run_sync(
                        rpc_selected_session_state,
                        target,
                    )
                else:
                    refreshed_entry_count = selected_entry_count
                    refreshed_name = None
            changed = RpcSessionNameChanged(
                command_id=command_id,
                session_id=change.session_id,
                session_path=change.path,
                previous_name=change.previous_name,
                name=change.name,
                entry_count=change.entry_count,
            )
            post_apply_events = (
                changed,
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="set_session_name",
                    ok=True,
                ),
            )
            finish_in_worker = False
            ok = True
        if cancel_scope.cancel_called and not ok:
            error = "RPC set_session_name command cancelled"
    except BaseException as exc:
        refreshed_history = None
        refreshed_entry_count = selected_entry_count
        refreshed_name = None
        selected_target = False
        post_apply_events = ()
        finish_in_worker = True
        if isinstance(
            exc,
            (anyio.get_cancelled_exc_class(), SessionNavigationCancelledError),
        ):
            error = "RPC set_session_name command cancelled"
        else:
            error = str(exc)
    finally:
        if finish_in_worker:
            write_event(
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="set_session_name",
                    ok=ok,
                    error=error,
                )
            )
        await send.send(
            command_completed_factory(
                command_id=command_id,
                command_type="set_session_name",
                ok=ok,
                history=refreshed_history,
                entry_count=refreshed_entry_count,
                session_name=refreshed_name,
                session_name_updated=ok and selected_target,
                post_apply_events=post_apply_events,
            )
        )
        await send.aclose()
