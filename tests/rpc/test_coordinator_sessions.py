from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from tests.rpc.coordinator_support import ignore_reject
from wisp.events import RpcSessionSelected, WispEvent
from wisp.messages import Message
from wisp.rpc.coordinator import (
    RpcCoordinator,
    _RpcCommandCompleted,
    _RpcDispatchResult,
    _RpcRunningCommand,
    _RpcSessionState,
)
from wisp.sessions.jsonl import JsonlSessionStore


def test_coordinator_applies_derived_session_from_async_completion(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = JsonlSessionStore(tmp_path).create()
        history = (Message(role="user", content="resumed"),)
        state = _RpcSessionState(session=None, history=(), entry_count=0)
        coordinator = RpcCoordinator(state)
        coordinator.running_command = _RpcRunningCommand(
            "clone",
            "clone_session",
            anyio.CancelScope(),
        )

        await coordinator.handle_event(
            _RpcCommandCompleted("clone", "clone_session", True, history, 1, session),
            dispatch=lambda _command, running: _RpcDispatchResult(running),
            reject=ignore_reject,
        )

        assert coordinator.running_command is None
        assert state.session is session
        assert state.history == history
        assert state.entry_count == 1

    anyio.run(scenario)


def test_coordinator_emits_post_apply_events_after_selecting_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = JsonlSessionStore(tmp_path).create()
        state = _RpcSessionState(session=None, history=(), entry_count=0)
        observed: list[tuple[WispEvent, object]] = []

        def write_event(event: WispEvent) -> None:
            observed.append((event, state.session))

        coordinator = RpcCoordinator(state, completion_event_writer=write_event)
        coordinator.running_command = _RpcRunningCommand(
            "select",
            "select_session",
            anyio.CancelScope(),
        )
        selected = RpcSessionSelected(
            command_id="select",
            session_id=session.session_id,
            session_path=session.path,
            entry_count=0,
        )

        await coordinator.handle_event(
            _RpcCommandCompleted(
                "select",
                "select_session",
                True,
                (),
                0,
                session,
                (selected,),
            ),
            dispatch=lambda _command, running: _RpcDispatchResult(running),
            reject=ignore_reject,
        )

        assert observed == [(selected, session)]

    anyio.run(scenario)


def test_coordinator_ignores_selected_session_on_failed_completion(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = JsonlSessionStore(tmp_path)
        previous = store.create()
        attempted = store.create()
        state = _RpcSessionState(session=previous, history=(), entry_count=0)
        coordinator = RpcCoordinator(state)
        coordinator.running_command = _RpcRunningCommand(
            "select",
            "select_session",
            anyio.CancelScope(),
        )

        await coordinator.handle_event(
            _RpcCommandCompleted("select", "select_session", False, (), 0, attempted),
            dispatch=lambda _command, running: _RpcDispatchResult(running),
            reject=ignore_reject,
        )

        assert coordinator.running_command is None
        assert state.session is previous

    anyio.run(scenario)


@pytest.mark.parametrize("command_type", ["select_session", "clone_session", "fork_session"])
@pytest.mark.parametrize("outcome", ["changed", "same", "failed"])
def test_session_grants_reset_only_after_successful_session_change(
    tmp_path: Path, command_type: str, outcome: str
) -> None:
    async def scenario() -> None:
        store = JsonlSessionStore(tmp_path)
        previous = store.create()
        selected = previous if outcome == "same" else store.create()
        state = _RpcSessionState(session=previous, history=(), entry_count=0)
        resets: list[object] = []
        coordinator = RpcCoordinator(
            state, session_grants_reset=lambda: resets.append(state.session)
        )
        coordinator.running_command = _RpcRunningCommand(
            "switch", command_type, anyio.CancelScope()
        )
        await coordinator.handle_event(
            _RpcCommandCompleted("switch", command_type, outcome != "failed", (), 0, selected),
            dispatch=lambda _command, running: _RpcDispatchResult(running),
            reject=ignore_reject,
        )
        assert resets == ([previous] if outcome == "changed" else [])
        assert state.session is (previous if outcome == "failed" else selected)

    anyio.run(scenario)
