from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import anyio

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


def test_coordinator_buffers_queue_commands_until_prompt_ready() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        dispatched: list[str] = []

        def dispatch(
            command: dict[str, object],
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command["id"])
            dispatched.append(command_id)
            if command["type"] != "prompt":
                return _RpcDispatchResult(running)
            return _RpcDispatchResult(_RpcRunningCommand(command_id, "prompt", anyio.CancelScope()))

        coordinator.handle_event(
            _RpcInputCommand({"id": "prompt", "type": "prompt"}),
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

        assert dispatched == ["prompt"]
        assert list(coordinator.pending_prompt_queue_commands) == [{"id": "steer", "type": "steer"}]

        coordinator.handle_event(
            _RpcPromptReady("prompt"),
            dispatch=dispatch,
            reject=lambda _command, _message: None,
            command_type=_command_type,
        )

        assert dispatched == ["prompt", "steer"]
        assert not coordinator.pending_prompt_queue_commands

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
