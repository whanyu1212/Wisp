from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import anyio
import pytest

from wisp.agent.messages import Message
from wisp.events import RpcSessionSelected, WispEvent
from wisp.rpc.coordinator import (
    RpcCoordinator,
    _RpcCommandCompleted,
    _RpcDispatchResult,
    _RpcInputClosed,
    _RpcInputCommand,
    _RpcPromptReady,
    _RpcRunningCommand,
    _RpcSessionState,
)
from wisp.sessions.jsonl import JsonlSessionStore


class _Receiver:
    def __init__(self, events: list[object]) -> None:
        self.events = deque(events)

    async def receive(self) -> object:
        await anyio.sleep(0)
        return self.events.popleft()

    def receive_nowait(self) -> object:
        if not self.events:
            raise anyio.WouldBlock
        return self.events.popleft()


def _command_type(command: dict[str, object]) -> str:
    value = command.get("type")
    return value if isinstance(value, str) else "unknown"


def test_coordinator_runs_queued_commands_in_fifo_order() -> None:
    async def scenario() -> None:
        history = (Message(role="user", content="done"),)
        receiver = _Receiver(
            [
                _RpcInputCommand({"id": "one", "type": "prompt"}),
                _RpcInputCommand({"id": "two", "type": "prompt"}),
                _RpcInputCommand({"id": "three", "type": "prompt"}),
                _RpcCommandCompleted("one", "prompt", True, history, 1),
                _RpcCommandCompleted("two", "prompt", True, history, 2),
                _RpcCommandCompleted("three", "prompt", True, history, 3),
                _RpcInputClosed(),
            ]
        )
        state = _RpcSessionState(session=None, history=(), entry_count=0)
        coordinator = RpcCoordinator(state)
        dispatched: list[str] = []

        def dispatch(
            command: dict[str, object],
            _running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command["id"])
            dispatched.append(command_id)
            return _RpcDispatchResult(_RpcRunningCommand(command_id, "prompt", anyio.CancelScope()))

        should_shutdown = await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=lambda _command, _message: None,
            command_type=_command_type,
        )

        assert should_shutdown is False
        assert dispatched == ["one", "two", "three"]
        assert state.history == history
        assert state.entry_count == 3

    anyio.run(scenario)


def test_coordinator_applies_new_session_reset_atomically(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = JsonlSessionStore(tmp_path).create()
        await session.append_message(Message(role="user", content="previous"))
        history = (Message(role="user", content="previous"),)
        state = _RpcSessionState(
            session=session,
            history=history,
            entry_count=3,
            name="Previous",
        )
        coordinator = RpcCoordinator(state)
        receiver = _Receiver(
            [
                _RpcInputCommand({"id": "new-1", "type": "new_session"}),
                _RpcInputClosed(),
            ]
        )

        await coordinator.run(
            receiver,
            dispatch=lambda _command, running: _RpcDispatchResult(
                running_command=running,
                reset_session=True,
            ),
            reject=lambda _command, _message: None,
            command_type=_command_type,
        )

        assert state.session is None
        assert state.history == ()
        assert state.entry_count == 0
        assert state.name is None
        assert session.path.is_file()

    anyio.run(scenario)


@pytest.mark.parametrize("command_type", ["get_session_stats", "get_messages"])
def test_coordinator_queues_new_session_behind_work_already_waiting_on_background_read(
    command_type: Literal["get_session_stats", "get_messages"],
) -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        background = _RpcRunningCommand("background", command_type, anyio.CancelScope())
        coordinator.running_command = background
        coordinator.queued_commands.append({"id": "prompt", "type": "prompt"})
        dispatched: list[str] = []

        coordinator.handle_event(
            _RpcInputCommand({"id": "new", "type": "new_session"}),
            dispatch=lambda command, running: (
                dispatched.append(str(command["id"])) or _RpcDispatchResult(running)
            ),
            reject=lambda _command, _message: None,
            command_type=_command_type,
        )

        assert dispatched == []
        assert list(coordinator.queued_commands) == [
            {"id": "prompt", "type": "prompt"},
            {"id": "new", "type": "new_session"},
        ]
        assert background.cancel_scope.cancel_called is False

    anyio.run(scenario)


@pytest.mark.parametrize(
    "command_type",
    ["get_messages", "get_sessions", "get_session_stats", "get_session_tree"],
)
def test_coordinator_queues_new_session_behind_active_ordered_read(
    command_type: Literal["get_messages", "get_sessions", "get_session_stats", "get_session_tree"],
) -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        background = _RpcRunningCommand("background", command_type, anyio.CancelScope())
        coordinator.running_command = background
        dispatched: list[str] = []

        coordinator.handle_event(
            _RpcInputCommand({"id": "new", "type": "new_session"}),
            dispatch=lambda command, running: (
                dispatched.append(str(command["id"])) or _RpcDispatchResult(running)
            ),
            reject=lambda _command, _message: None,
            command_type=_command_type,
        )

        assert dispatched == []
        assert list(coordinator.queued_commands) == [{"id": "new", "type": "new_session"}]
        assert background.cancel_scope.cancel_called is False

    anyio.run(scenario)


def test_coordinator_queues_new_session_behind_work_waiting_on_stats_async() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        stats = _RpcRunningCommand("stats", "get_session_stats", anyio.CancelScope())
        coordinator.running_command = stats
        coordinator.queued_commands.append({"id": "prompt", "type": "prompt"})
        dispatched: list[str] = []

        async def dispatch(
            command: dict[str, object],
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            dispatched.append(str(command["id"]))
            return _RpcDispatchResult(running)

        async def reject(_command: dict[str, object], _message: str) -> None:
            raise AssertionError("new_session should be queued, not rejected")

        await coordinator.handle_event_async(
            _RpcInputCommand({"id": "new", "type": "new_session"}),
            dispatch=dispatch,
            reject=reject,
            command_type=_command_type,
        )

        assert dispatched == []
        assert list(coordinator.queued_commands) == [
            {"id": "prompt", "type": "prompt"},
            {"id": "new", "type": "new_session"},
        ]
        assert stats.cancel_scope.cancel_called is False

    anyio.run(scenario)


def test_coordinator_dispatches_control_commands_while_active() -> None:
    async def scenario() -> None:
        receiver = _Receiver(
            [
                _RpcInputCommand({"id": "prompt", "type": "prompt"}),
                _RpcInputCommand({"id": "queued", "type": "prompt"}),
                _RpcInputCommand({"id": "messages", "type": "get_messages"}),
                _RpcInputCommand({"id": "sessions", "type": "get_sessions"}),
                _RpcInputCommand({"id": "select", "type": "select_session"}),
                _RpcInputCommand({"id": "clone", "type": "clone_session"}),
                _RpcInputCommand({"id": "fork", "type": "fork_session", "entry_id": "entry"}),
                _RpcInputCommand({"id": "tree", "type": "get_session_tree"}),
                _RpcInputCommand(
                    {"id": "navigate", "type": "navigate_session_tree", "entry_id": "entry"}
                ),
                _RpcInputCommand({"id": "unrevert", "type": "unrevert_session_tree"}),
                _RpcInputCommand({"id": "commands", "type": "get_commands"}),
                _RpcInputCommand({"id": "approval", "type": "approval"}),
                _RpcInputCommand({"id": "steer", "type": "steer"}),
                _RpcInputCommand({"id": "follow", "type": "follow_up"}),
                _RpcInputCommand({"id": "state", "type": "get_queue_state"}),
                _RpcInputCommand({"id": "mode", "type": "set_queue_mode"}),
                _RpcInputCommand({"id": "pop", "type": "pop_queue"}),
                _RpcInputCommand({"id": "clear", "type": "clear_queue"}),
                _RpcPromptReady("prompt"),
                _RpcCommandCompleted("prompt", "prompt", True, (), 1),
                _RpcCommandCompleted("queued", "prompt", True, (), 2),
                _RpcCommandCompleted("messages", "get_messages", True, (), 2),
                _RpcCommandCompleted("sessions", "get_sessions", True, (), 2),
                _RpcCommandCompleted("select", "select_session", True, (), 2),
                _RpcCommandCompleted("clone", "clone_session", True, (), 2),
                _RpcCommandCompleted("fork", "fork_session", True, (), 2),
                _RpcCommandCompleted("tree", "get_session_tree", True, (), 2),
                _RpcCommandCompleted("navigate", "navigate_session_tree", True, (), 2),
                _RpcCommandCompleted("unrevert", "unrevert_session_tree", True, (), 2),
                _RpcInputClosed(),
            ]
        )
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        dispatched: list[str] = []

        def dispatch(
            command: dict[str, object],
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command["id"])
            dispatched.append(command_id)
            if command["type"] not in {
                "prompt",
                "get_messages",
                "get_sessions",
                "select_session",
                "clone_session",
                "fork_session",
                "get_session_tree",
                "navigate_session_tree",
                "unrevert_session_tree",
                "set_session_name",
            }:
                return _RpcDispatchResult(running)
            return _RpcDispatchResult(
                _RpcRunningCommand(command_id, str(command["type"]), anyio.CancelScope())
            )

        await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=lambda _command, _message: None,
            command_type=_command_type,
        )

        assert dispatched == [
            "prompt",
            "commands",
            "approval",
            "steer",
            "follow",
            "state",
            "mode",
            "pop",
            "clear",
            "queued",
            "messages",
            "sessions",
            "select",
            "clone",
            "fork",
            "tree",
            "navigate",
            "unrevert",
        ]

    anyio.run(scenario)


def test_coordinator_dispatches_busy_configure_for_rejection_async() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        running = _RpcRunningCommand("prompt", "prompt", anyio.CancelScope())
        coordinator.running_command = running
        dispatched: list[dict[str, object]] = []

        async def dispatch(
            command: dict[str, object],
            active: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            assert active is running
            dispatched.append(command)
            return _RpcDispatchResult(active)

        async def reject(_command: dict[str, object], _message: str) -> None:
            raise AssertionError("configure should reach the executor for busy rejection")

        await coordinator.handle_event_async(
            _RpcInputCommand({"id": "configure", "type": "configure"}),
            dispatch=dispatch,
            reject=reject,
            command_type=_command_type,
        )

        assert dispatched == [{"id": "configure", "type": "configure"}]
        assert not coordinator.queued_commands

    anyio.run(scenario)


@pytest.mark.parametrize("run_type", ["prompt", "init"])
def test_coordinator_buffers_queue_commands_until_prompt_ready(run_type: str) -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        dispatched: list[str] = []

        def dispatch(
            command: dict[str, object],
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command["id"])
            dispatched.append(command_id)
            if command["type"] != run_type:
                return _RpcDispatchResult(running)
            return _RpcDispatchResult(_RpcRunningCommand(command_id, run_type, anyio.CancelScope()))

        coordinator.handle_event(
            _RpcInputCommand({"id": "run", "type": run_type}),
            dispatch=dispatch,
            reject=lambda _command, _message: None,
            command_type=_command_type,
        )
        coordinator.handle_event(
            _RpcInputCommand({"id": "steer", "type": "steer"}),
            dispatch=dispatch,
            reject=lambda _command, _message: None,
            command_type=_command_type,
        )

        assert dispatched == ["run"]
        assert list(coordinator.pending_prompt_queue_commands) == [{"id": "steer", "type": "steer"}]

        coordinator.handle_event(
            _RpcPromptReady("run"),
            dispatch=dispatch,
            reject=lambda _command, _message: None,
            command_type=_command_type,
        )

        assert dispatched == ["run", "steer"]
        assert not coordinator.pending_prompt_queue_commands

    anyio.run(scenario)


def test_coordinator_async_buffers_init_queue_until_ready() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        coordinator.running_command = _RpcRunningCommand("init", "init", anyio.CancelScope())
        dispatched: list[str] = []

        async def dispatch(
            command: dict[str, object],
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            dispatched.append(str(command["id"]))
            return _RpcDispatchResult(running)

        async def reject(_command: dict[str, object], _message: str) -> None:
            raise AssertionError("command unexpectedly rejected")

        await coordinator.handle_event_async(
            _RpcInputCommand({"id": "follow-up", "type": "follow_up"}),
            dispatch=dispatch,
            reject=reject,
            command_type=_command_type,
        )
        assert dispatched == []

        await coordinator.handle_event_async(
            _RpcPromptReady("init"),
            dispatch=dispatch,
            reject=reject,
            command_type=_command_type,
        )
        assert dispatched == ["follow-up"]

    anyio.run(scenario)


def test_coordinator_dispatches_new_session_while_init_is_running() -> None:
    async def scenario() -> None:
        sync_coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        sync_coordinator.running_command = _RpcRunningCommand("init", "init", anyio.CancelScope())
        sync_dispatched: list[str] = []

        def sync_dispatch(
            command: dict[str, object],
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            sync_dispatched.append(str(command["id"]))
            return _RpcDispatchResult(running)

        sync_coordinator.handle_event(
            _RpcInputCommand({"id": "new-sync", "type": "new_session"}),
            dispatch=sync_dispatch,
            reject=lambda _command, _message: None,
            command_type=_command_type,
        )
        assert sync_dispatched == ["new-sync"]
        assert not sync_coordinator.queued_commands

        async_coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        async_coordinator.running_command = _RpcRunningCommand("init", "init", anyio.CancelScope())
        async_dispatched: list[str] = []

        async def async_dispatch(
            command: dict[str, object],
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            async_dispatched.append(str(command["id"]))
            return _RpcDispatchResult(running)

        async def reject(_command: dict[str, object], _message: str) -> None:
            raise AssertionError("command unexpectedly rejected")

        await async_coordinator.handle_event_async(
            _RpcInputCommand({"id": "new-async", "type": "new_session"}),
            dispatch=async_dispatch,
            reject=reject,
            command_type=_command_type,
        )
        assert async_dispatched == ["new-async"]
        assert not async_coordinator.queued_commands

    anyio.run(scenario)


def test_coordinator_state_bypasses_active_prompt_without_draining_pending_queue() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        dispatched: list[tuple[str, str | None]] = []

        def dispatch(
            command: dict[str, object],
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command["id"])
            dispatched.append((command_id, running.command_id if running is not None else None))
            if command["type"] == "prompt":
                return _RpcDispatchResult(
                    _RpcRunningCommand(command_id, "prompt", anyio.CancelScope())
                )
            return _RpcDispatchResult(running)

        def handle(command: dict[str, object]) -> None:
            coordinator.handle_event(
                _RpcInputCommand(command),
                dispatch=dispatch,
                reject=lambda _command, _message: None,
                command_type=_command_type,
            )

        handle({"id": "prompt", "type": "prompt"})
        handle({"id": "steer", "type": "steer"})
        handle({"id": "queued", "type": "compact"})
        handle({"id": "state-before", "type": "get_state"})
        handle({"id": "commands-before", "type": "get_commands"})

        assert dispatched == [
            ("prompt", None),
            ("state-before", "prompt"),
            ("commands-before", "prompt"),
        ]
        assert list(coordinator.pending_prompt_queue_commands) == [{"id": "steer", "type": "steer"}]
        assert list(coordinator.queued_commands) == [{"id": "queued", "type": "compact"}]

        coordinator.handle_event(
            _RpcPromptReady("prompt"),
            dispatch=dispatch,
            reject=lambda _command, _message: None,
            command_type=_command_type,
        )
        handle({"id": "state-after", "type": "get_state"})
        handle({"id": "commands-after", "type": "get_commands"})

        assert dispatched == [
            ("prompt", None),
            ("state-before", "prompt"),
            ("commands-before", "prompt"),
            ("steer", "prompt"),
            ("state-after", "prompt"),
            ("commands-after", "prompt"),
        ]
        assert not coordinator.pending_prompt_queue_commands
        assert list(coordinator.queued_commands) == [{"id": "queued", "type": "compact"}]
        assert coordinator.running_command is not None
        assert coordinator.running_command.command_id == "prompt"

    anyio.run(scenario)


def test_coordinator_state_bypasses_active_read_commands() -> None:
    async def scenario() -> None:
        async def assert_bypasses(
            active_type: Literal[
                "compact",
                "get_session_stats",
                "get_messages",
                "get_sessions",
                "select_session",
                "clone_session",
                "fork_session",
                "get_session_tree",
                "navigate_session_tree",
                "unrevert_session_tree",
                "set_session_name",
            ],
        ) -> None:
            coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
            running_command = _RpcRunningCommand(
                command_id=f"active-{active_type}",
                command_type=active_type,
                cancel_scope=anyio.CancelScope(),
            )
            coordinator.running_command = running_command
            dispatched: list[tuple[str, str | None]] = []

            def dispatch(
                command: dict[str, object],
                running: _RpcRunningCommand | None,
            ) -> _RpcDispatchResult:
                dispatched.append(
                    (
                        str(command["id"]),
                        running.command_id if running is not None else None,
                    )
                )
                return _RpcDispatchResult(running)

            coordinator.handle_event(
                _RpcInputCommand({"id": "state", "type": "get_state"}),
                dispatch=dispatch,
                reject=lambda _command, _message: None,
                command_type=_command_type,
            )
            coordinator.handle_event(
                _RpcInputCommand({"id": "commands", "type": "get_commands"}),
                dispatch=dispatch,
                reject=lambda _command, _message: None,
                command_type=_command_type,
            )

            assert dispatched == [
                ("state", running_command.command_id),
                ("commands", running_command.command_id),
            ]
            assert coordinator.running_command is running_command
            assert not coordinator.pending_prompt_queue_commands
            assert not coordinator.queued_commands

        await assert_bypasses("compact")
        await assert_bypasses("get_session_stats")
        await assert_bypasses("get_messages")
        await assert_bypasses("get_sessions")
        await assert_bypasses("select_session")
        await assert_bypasses("clone_session")
        await assert_bypasses("fork_session")
        await assert_bypasses("get_session_tree")
        await assert_bypasses("navigate_session_tree")
        await assert_bypasses("unrevert_session_tree")
        await assert_bypasses("set_session_name")

    anyio.run(scenario)


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

        coordinator.handle_event(
            _RpcCommandCompleted("clone", "clone_session", True, history, 1, session),
            dispatch=lambda _command, running: _RpcDispatchResult(running),
            reject=lambda _command, _message: None,
            command_type=_command_type,
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

        coordinator.handle_event(
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
            reject=lambda _command, _message: None,
            command_type=_command_type,
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

        coordinator.handle_event(
            _RpcCommandCompleted("select", "select_session", False, (), 0, attempted),
            dispatch=lambda _command, running: _RpcDispatchResult(running),
            reject=lambda _command, _message: None,
            command_type=_command_type,
        )

        assert coordinator.running_command is None
        assert state.session is previous

    anyio.run(scenario)


def test_coordinator_ignores_stale_prompt_readiness() -> None:
    async def scenario() -> None:
        receiver = _Receiver(
            [
                _RpcInputCommand({"id": "prompt", "type": "prompt"}),
                _RpcInputCommand({"id": "steer", "type": "steer"}),
                _RpcPromptReady("stale"),
                _RpcCommandCompleted("prompt", "prompt", True, (), 1),
                _RpcInputClosed(),
            ]
        )
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        dispatched: list[tuple[str, str | None]] = []

        def dispatch(
            command: dict[str, object],
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command["id"])
            dispatched.append((command_id, running.command_id if running is not None else None))
            if command["type"] != "prompt":
                return _RpcDispatchResult(running)
            return _RpcDispatchResult(_RpcRunningCommand(command_id, "prompt", anyio.CancelScope()))

        await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=lambda _command, _message: None,
            command_type=_command_type,
        )

        assert dispatched == [("prompt", None), ("steer", None)]

    anyio.run(scenario)


def test_coordinator_does_not_retarget_queue_commands_when_prompt_fails_before_ready() -> None:
    async def scenario() -> None:
        receiver = _Receiver(
            [
                _RpcInputCommand({"id": "first", "type": "prompt"}),
                _RpcInputCommand({"id": "second", "type": "prompt"}),
                _RpcInputCommand({"id": "steer", "type": "steer"}),
                _RpcCommandCompleted("first", "prompt", False, (), 0),
                _RpcPromptReady("second"),
                _RpcCommandCompleted("second", "prompt", True, (), 1),
                _RpcInputClosed(),
            ]
        )
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        dispatched: list[tuple[str, str | None]] = []

        def dispatch(
            command: dict[str, object],
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command["id"])
            dispatched.append((command_id, running.command_id if running is not None else None))
            if command["type"] != "prompt":
                return _RpcDispatchResult(running)
            return _RpcDispatchResult(_RpcRunningCommand(command_id, "prompt", anyio.CancelScope()))

        await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=lambda _command, _message: None,
            command_type=_command_type,
        )

        assert dispatched == [
            ("first", None),
            ("steer", None),
            ("second", None),
        ]

    anyio.run(scenario)


def test_coordinator_rejects_duplicate_running_and_allows_reuse_after_completion() -> None:
    async def scenario() -> None:
        receiver = _Receiver(
            [
                _RpcInputCommand({"id": "same", "type": "prompt"}),
                _RpcInputCommand({"id": "same", "type": "prompt"}),
                _RpcCommandCompleted("same", "prompt", True, (), 1),
                _RpcInputCommand({"id": "same", "type": "prompt"}),
                _RpcCommandCompleted("same", "prompt", True, (), 2),
                _RpcInputClosed(),
            ]
        )
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        dispatched: list[str] = []
        rejected: list[tuple[dict[str, object], str]] = []

        def dispatch(
            command: dict[str, object],
            _running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command["id"])
            dispatched.append(command_id)
            return _RpcDispatchResult(_RpcRunningCommand(command_id, "prompt", anyio.CancelScope()))

        await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=lambda command, message: rejected.append((command, message)),
            command_type=_command_type,
        )

        assert dispatched == ["same", "same"]
        assert rejected == [({"type": "prompt"}, "RPC command id is already outstanding: same")]
        assert coordinator.session_state.entry_count == 2

    anyio.run(scenario)


def test_coordinator_rejects_duplicate_ids_in_both_queues() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        coordinator.running_command = _RpcRunningCommand("active", "prompt", anyio.CancelScope())
        coordinator.pending_prompt_queue_commands.append(
            {"id": "pending", "type": "steer", "content": "redirect"}
        )
        coordinator.queued_commands.append({"id": "queued", "type": "prompt", "prompt": "later"})
        rejected: list[tuple[dict[str, object], str]] = []

        for command_id in ("pending", "queued"):
            coordinator.handle_event(
                _RpcInputCommand({"id": command_id, "type": "prompt", "prompt": "duplicate"}),
                dispatch=lambda _command, running: _RpcDispatchResult(running),
                reject=lambda command, message: rejected.append((command, message)),
                command_type=_command_type,
            )

        assert rejected == [
            (
                {"type": "prompt", "prompt": "duplicate"},
                "RPC command id is already outstanding: pending",
            ),
            (
                {"type": "prompt", "prompt": "duplicate"},
                "RPC command id is already outstanding: queued",
            ),
        ]
        assert [command["id"] for command in coordinator.pending_prompt_queue_commands] == [
            "pending"
        ]
        assert [command["id"] for command in coordinator.queued_commands] == ["queued"]

    anyio.run(scenario)


def test_coordinator_rejects_duplicate_running_id_async() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        running = _RpcRunningCommand("same", "prompt", anyio.CancelScope())
        coordinator.running_command = running
        rejected: list[tuple[dict[str, object], str]] = []

        async def dispatch(
            _command: dict[str, object],
            _running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            raise AssertionError("duplicate command must not be dispatched")

        async def reject(command: dict[str, object], message: str) -> None:
            rejected.append((command, message))

        await coordinator.handle_event_async(
            _RpcInputCommand({"id": "same", "type": "get_state"}),
            dispatch=dispatch,
            reject=reject,
            command_type=_command_type,
        )

        assert coordinator.running_command is running
        assert rejected == [({"type": "get_state"}, "RPC command id is already outstanding: same")]

    anyio.run(scenario)


def test_coordinator_rejects_commands_beyond_its_queue_bound() -> None:
    async def scenario() -> None:
        receiver = _Receiver(
            [
                _RpcInputCommand({"id": "active", "type": "prompt"}),
                _RpcInputCommand({"id": "queued", "type": "prompt"}),
                _RpcInputCommand({"id": "overflow", "type": "prompt"}),
                _RpcCommandCompleted("active", "prompt", True, (), 1),
                _RpcCommandCompleted("queued", "prompt", True, (), 2),
                _RpcInputClosed(),
            ]
        )
        coordinator = RpcCoordinator(
            _RpcSessionState(None, (), 0),
            max_queued_commands=1,
        )
        rejected: list[tuple[str, str]] = []

        def dispatch(
            command: dict[str, object],
            _running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command["id"])
            return _RpcDispatchResult(_RpcRunningCommand(command_id, "prompt", anyio.CancelScope()))

        await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=lambda command, message: rejected.append((str(command["id"]), message)),
            command_type=_command_type,
        )

        assert rejected == [
            ("overflow", "RPC command queue is full while another RPC command is running")
        ]

    anyio.run(scenario)


def test_coordinator_ignores_stale_completion_and_closes_decisions_once() -> None:
    async def scenario() -> None:
        closed = 0

        def on_closed() -> None:
            nonlocal closed
            closed += 1

        receiver = _Receiver(
            [
                _RpcInputCommand({"id": "active", "type": "prompt"}),
                _RpcCommandCompleted("stale", "prompt", True, (), 99),
                _RpcInputClosed(),
                _RpcInputClosed(),
                _RpcCommandCompleted("active", "prompt", True, (), 1),
            ]
        )
        coordinator = RpcCoordinator(
            _RpcSessionState(None, (), 0),
            input_closed_handlers=(on_closed,),
        )

        def dispatch(
            command: dict[str, object],
            _running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            return _RpcDispatchResult(
                _RpcRunningCommand(str(command["id"]), "prompt", anyio.CancelScope())
            )

        await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=lambda _command, _message: None,
            command_type=_command_type,
        )

        assert closed == 1
        assert coordinator.session_state.entry_count == 1

    anyio.run(scenario)


def test_coordinator_drains_buffered_cancel_before_queued_shutdown() -> None:
    async def scenario() -> None:
        receiver = _Receiver(
            [
                _RpcInputCommand({"id": "prompt", "type": "prompt"}),
                _RpcInputCommand({"id": "shutdown", "type": "shutdown"}),
                _RpcCommandCompleted("prompt", "prompt", True, (), 1),
                _RpcInputCommand({"id": "cancel", "type": "cancel", "target_id": "shutdown"}),
                _RpcInputClosed(),
            ]
        )
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        dispatched: list[str] = []

        def dispatch(
            command: dict[str, object],
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command["id"])
            dispatched.append(command_id)
            if command.get("type") == "prompt":
                return _RpcDispatchResult(
                    _RpcRunningCommand(command_id, "prompt", anyio.CancelScope())
                )
            if command.get("type") == "cancel":
                assert coordinator.cancel("shutdown").outcome == "queued"
            return _RpcDispatchResult(running)

        assert (
            await coordinator.run(
                receiver,
                dispatch=dispatch,
                reject=lambda _command, _message: None,
                command_type=_command_type,
            )
        ) is False
        assert dispatched == ["prompt", "cancel"]

    anyio.run(scenario)


def test_coordinator_drains_buffered_cancel_before_queued_shutdown_async() -> None:
    async def scenario() -> None:
        receiver = _Receiver(
            [
                _RpcInputCommand({"id": "prompt", "type": "prompt"}),
                _RpcInputCommand({"id": "shutdown", "type": "shutdown"}),
                _RpcCommandCompleted("prompt", "prompt", True, (), 1),
                _RpcInputCommand({"id": "cancel", "type": "cancel", "target_id": "shutdown"}),
                _RpcInputClosed(),
            ]
        )
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        dispatched: list[str] = []

        async def dispatch(
            command: dict[str, object],
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command["id"])
            dispatched.append(command_id)
            if command.get("type") == "prompt":
                return _RpcDispatchResult(
                    _RpcRunningCommand(command_id, "prompt", anyio.CancelScope())
                )
            if command.get("type") == "cancel":
                assert coordinator.cancel("shutdown").outcome == "queued"
            return _RpcDispatchResult(running)

        async def reject(_command: dict[str, object], _message: str) -> None:
            return None

        assert (
            await coordinator.run_async(
                receiver,
                dispatch=dispatch,
                reject=reject,
                command_type=_command_type,
            )
        ) is False
        assert dispatched == ["prompt", "cancel"]

    anyio.run(scenario)


def test_coordinator_awaits_completion_events_before_next_async_dispatch() -> None:
    async def scenario() -> None:
        render_started = anyio.Event()
        release_render = anyio.Event()
        next_dispatched = anyio.Event()
        completion_event = RpcSessionSelected(
            command_id="select",
            session_id="session",
            session_path=Path("session.jsonl"),
            entry_count=1,
        )

        async def render_completion(events: tuple[WispEvent, ...]) -> None:
            assert events == (completion_event,)
            render_started.set()
            await release_render.wait()

        receiver = _Receiver(
            [
                _RpcCommandCompleted(
                    "select",
                    "select_session",
                    True,
                    (),
                    1,
                    post_apply_events=(completion_event,),
                ),
                _RpcCommandCompleted("next", "get_sessions", True, (), 1),
                _RpcInputClosed(),
            ]
        )
        coordinator = RpcCoordinator(
            _RpcSessionState(None, (), 0),
            completion_event_renderer=render_completion,
        )
        coordinator.running_command = _RpcRunningCommand(
            "select",
            "select_session",
            anyio.CancelScope(),
        )
        coordinator.queued_commands.append({"id": "next", "type": "get_sessions"})

        async def dispatch(
            command: dict[str, object],
            _running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            next_dispatched.set()
            return _RpcDispatchResult(
                _RpcRunningCommand(
                    str(command["id"]),
                    "get_sessions",
                    anyio.CancelScope(),
                )
            )

        async def reject(_command: dict[str, object], _message: str) -> None:
            return None

        async def run_coordinator() -> None:
            await coordinator.run_async(
                receiver,
                dispatch=dispatch,
                reject=reject,
                command_type=_command_type,
            )

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run_coordinator)
            await render_started.wait()
            await anyio.sleep(0)
            assert next_dispatched.is_set() is False
            release_render.set()
            await next_dispatched.wait()

    anyio.run(scenario)


def test_coordinator_drains_buffered_control_before_idle_shutdown_async() -> None:
    async def scenario() -> None:
        receiver = _Receiver(
            [
                _RpcInputCommand({"id": "shutdown", "type": "shutdown"}),
                _RpcInputCommand({"id": "cancel", "type": "cancel", "target_id": "missing"}),
            ]
        )
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        dispatched: list[str] = []

        async def dispatch(
            command: dict[str, object],
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command["id"])
            dispatched.append(command_id)
            return _RpcDispatchResult(
                running,
                should_shutdown=command.get("type") == "shutdown",
            )

        async def reject(_command: dict[str, object], _message: str) -> None:
            return None

        assert (
            await coordinator.run_async(
                receiver,
                dispatch=dispatch,
                reject=reject,
                command_type=_command_type,
            )
        ) is True
        assert dispatched == ["cancel", "shutdown"]

    anyio.run(scenario)


def test_coordinator_returns_immediately_after_shutdown_dispatch() -> None:
    async def scenario() -> None:
        receiver = _Receiver(
            [
                _RpcInputCommand({"id": "shutdown", "type": "shutdown"}),
                _RpcInputCommand({"id": "unreached", "type": "prompt"}),
            ]
        )
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))

        should_shutdown = await coordinator.run(
            receiver,
            dispatch=lambda _command, running: _RpcDispatchResult(
                running,
                should_shutdown=True,
            ),
            reject=lambda _command, _message: None,
            command_type=_command_type,
        )

        assert should_shutdown is True
        assert len(receiver.events) == 1

    anyio.run(scenario)


def test_coordinator_accepts_compatibility_event_types() -> None:
    @dataclass(frozen=True)
    class CompatInputClosed:
        pass

    @dataclass(frozen=True)
    class CompatCommandCompleted:
        command_id: str
        command_type: Literal["prompt", "compact", "get_session_stats"]
        ok: bool
        history: tuple[Message, ...] | None
        entry_count: int

    async def scenario() -> None:
        receiver = _Receiver(
            [
                _RpcInputCommand({"id": "active", "type": "prompt"}),
                CompatInputClosed(),
                CompatCommandCompleted("active", "prompt", True, (), 1),
            ]
        )
        coordinator = RpcCoordinator(
            _RpcSessionState(None, (), 0),
            input_closed_type=CompatInputClosed,
            command_completed_type=CompatCommandCompleted,
        )

        def dispatch(
            command: dict[str, object],
            _running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            return _RpcDispatchResult(
                _RpcRunningCommand(str(command["id"]), "prompt", anyio.CancelScope())
            )

        await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=lambda _command, _message: None,
            command_type=_command_type,
        )

        assert coordinator.session_state.entry_count == 1

    anyio.run(scenario)


def test_coordinator_owns_running_and_queued_cancellation() -> None:
    async def scenario() -> None:
        state = _RpcSessionState(None, (), 0)
        coordinator = RpcCoordinator(state)
        active_scope = anyio.CancelScope()
        coordinator.running_command = _RpcRunningCommand("active", "prompt", active_scope)
        pending = {"id": "pending", "type": "steer"}
        coordinator.pending_prompt_queue_commands.append(pending)
        queued = {"id": "queued", "type": "prompt"}
        coordinator.queued_commands.extend([queued, {"id": "later", "type": "prompt"}])

        active_result = coordinator.cancel("active")
        pending_result = coordinator.cancel("pending")
        queued_result = coordinator.cancel("queued")
        missing_result = coordinator.cancel("missing")

        assert active_result.outcome == "running"
        assert active_scope.cancel_called is True
        assert pending_result.outcome == "queued"
        assert pending_result.command is pending
        assert not coordinator.pending_prompt_queue_commands
        assert queued_result.outcome == "queued"
        assert queued_result.command is queued
        assert list(coordinator.queued_commands) == [{"id": "later", "type": "prompt"}]
        assert missing_result.outcome == "missing"

    anyio.run(scenario)
