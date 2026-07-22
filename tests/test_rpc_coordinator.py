from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal

import anyio

from wisp.agent.messages import Message
from wisp.cli.rpc_coordinator import (
    RpcCoordinator,
    _RpcCommandCompleted,
    _RpcDispatchResult,
    _RpcInputClosed,
    _RpcInputCommand,
    _RpcRunningCommand,
    _RpcSessionState,
)


class _Receiver:
    def __init__(self, events: list[object]) -> None:
        self.events = deque(events)

    async def receive(self) -> object:
        await anyio.sleep(0)
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
                _RpcInputCommand({"id": "approval", "type": "approval"}),
                _RpcCommandCompleted("prompt", "prompt", True, (), 1),
                _RpcCommandCompleted("queued", "prompt", True, (), 2),
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
            if command["type"] == "approval":
                return _RpcDispatchResult(running)
            return _RpcDispatchResult(_RpcRunningCommand(command_id, "prompt", anyio.CancelScope()))

        await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=lambda _command, _message: None,
            command_type=_command_type,
        )

        assert dispatched == ["prompt", "approval", "queued"]

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
        queued = {"id": "queued", "type": "prompt"}
        coordinator.queued_commands.extend([queued, {"id": "later", "type": "prompt"}])

        active_result = coordinator.cancel("active")
        queued_result = coordinator.cancel("queued")
        missing_result = coordinator.cancel("missing")

        assert active_result.outcome == "running"
        assert active_scope.cancel_called is True
        assert queued_result.outcome == "queued"
        assert queued_result.command is queued
        assert list(coordinator.queued_commands) == [{"id": "later", "type": "prompt"}]
        assert missing_result.outcome == "missing"

    anyio.run(scenario)
