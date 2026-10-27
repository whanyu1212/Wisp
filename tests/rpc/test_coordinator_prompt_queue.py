from __future__ import annotations

import anyio
import pytest

from tests.rpc.coordinator_support import (
    Receiver,
    expected_commands,
    ignore_reject,
    input_command,
)
from wisp.rpc.commands import ParsedRpcCommand
from wisp.rpc.coordinator import (
    RpcCoordinator,
    _RpcCommandCompleted,
    _RpcDispatchResult,
    _RpcInputClosed,
    _RpcPromptReady,
    _RpcRunningCommand,
    _RpcSessionState,
)


@pytest.mark.parametrize("run_type", ["prompt", "init"])
def test_coordinator_buffers_queue_commands_until_prompt_ready(run_type: str) -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        dispatched: list[str] = []

        async def dispatch(
            command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command.command_id)
            dispatched.append(command_id)
            if command.command_type != run_type:
                return _RpcDispatchResult(running)
            return _RpcDispatchResult(_RpcRunningCommand(command_id, run_type, anyio.CancelScope()))

        await coordinator.handle_event(
            input_command(
                {"id": "run", "type": run_type, **({"prompt": ""} if run_type == "prompt" else {})}
            ),
            dispatch=dispatch,
            reject=ignore_reject,
        )
        await coordinator.handle_event(
            input_command({"content": "", "id": "steer", "type": "steer"}),
            dispatch=dispatch,
            reject=ignore_reject,
        )

        assert dispatched == ["run"]
        assert list(coordinator.pending_prompt_queue_commands) == expected_commands(
            [{"content": "", "id": "steer", "type": "steer"}]
        )

        await coordinator.handle_event(
            _RpcPromptReady("run"),
            dispatch=dispatch,
            reject=ignore_reject,
        )

        assert dispatched == ["run", "steer"]
        assert not coordinator.pending_prompt_queue_commands

    anyio.run(scenario)


def test_coordinator_ignores_stale_prompt_readiness() -> None:
    async def scenario() -> None:
        receiver = Receiver(
            [
                input_command({"prompt": "", "id": "prompt", "type": "prompt"}),
                input_command({"content": "", "id": "steer", "type": "steer"}),
                _RpcPromptReady("stale"),
                _RpcCommandCompleted("prompt", "prompt", True, (), 1),
                _RpcInputClosed(),
            ]
        )
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        dispatched: list[tuple[str, str | None]] = []

        async def dispatch(
            command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command.command_id)
            dispatched.append((command_id, running.command_id if running is not None else None))
            if command.command_type != "prompt":
                return _RpcDispatchResult(running)
            return _RpcDispatchResult(_RpcRunningCommand(command_id, "prompt", anyio.CancelScope()))

        await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=ignore_reject,
        )

        assert dispatched == [("prompt", None), ("steer", None)]

    anyio.run(scenario)


def test_coordinator_does_not_retarget_queue_commands_when_prompt_fails_before_ready() -> None:
    async def scenario() -> None:
        receiver = Receiver(
            [
                input_command({"prompt": "", "id": "first", "type": "prompt"}),
                input_command({"prompt": "", "id": "second", "type": "prompt"}),
                input_command({"content": "", "id": "steer", "type": "steer"}),
                _RpcCommandCompleted("first", "prompt", False, (), 0),
                _RpcPromptReady("second"),
                _RpcCommandCompleted("second", "prompt", True, (), 1),
                _RpcInputClosed(),
            ]
        )
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        dispatched: list[tuple[str, str | None]] = []

        async def dispatch(
            command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command.command_id)
            dispatched.append((command_id, running.command_id if running is not None else None))
            if command.command_type != "prompt":
                return _RpcDispatchResult(running)
            return _RpcDispatchResult(_RpcRunningCommand(command_id, "prompt", anyio.CancelScope()))

        await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=ignore_reject,
        )

        assert dispatched == [
            ("first", None),
            ("steer", None),
            ("second", None),
        ]

    anyio.run(scenario)
