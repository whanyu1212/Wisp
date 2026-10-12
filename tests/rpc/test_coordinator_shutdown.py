from __future__ import annotations

import anyio

from tests.rpc.coordinator_support import (
    Receiver,
    expected_commands,
    expected_rejections,
    ignore_reject,
    input_command,
)
from wisp.rpc.commands import ParsedRpcCommand
from wisp.rpc.coordinator import (
    RpcCoordinator,
    _RpcCommandCompleted,
    _RpcDispatchResult,
    _RpcInputClosed,
    _RpcRunningCommand,
    _RpcSessionState,
)


def test_coordinator_cancels_device_code_when_input_closes() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        cancel_scope = anyio.CancelScope()
        coordinator.running_command = _RpcRunningCommand(
            "device-code-1",
            "begin_device_code",
            cancel_scope,
        )

        await coordinator.handle_event(
            _RpcInputClosed(),
            dispatch=lambda _command, running: _RpcDispatchResult(running),
            reject=ignore_reject,
        )

        assert cancel_scope.cancel_called

    anyio.run(scenario)


def test_coordinator_rejects_queued_device_code_when_input_closes() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        coordinator.running_command = _RpcRunningCommand(
            "prompt-1",
            "prompt",
            anyio.CancelScope(),
        )
        device_code = {
            "id": "device-code-1",
            "type": "begin_device_code",
            "provider": "openai-codex",
        }
        rejected: list[tuple[ParsedRpcCommand, str]] = []

        async def reject(command: ParsedRpcCommand, message: str) -> None:
            rejected.append((command, message))

        await coordinator.handle_event(
            input_command(device_code),
            dispatch=lambda _command, running: _RpcDispatchResult(running),
            reject=reject,
        )
        assert list(coordinator.queued_commands) == expected_commands([device_code])

        await coordinator.handle_event(
            _RpcInputClosed(),
            dispatch=lambda _command, running: _RpcDispatchResult(running),
            reject=reject,
        )

        assert list(coordinator.queued_commands) == expected_commands([])
        assert coordinator._queued_command_bytes == 0
        assert list(rejected) == expected_rejections(
            [(device_code, "RPC command cancelled: device-code-1")]
        )

    anyio.run(scenario)


def test_coordinator_drains_buffered_cancel_before_queued_shutdown() -> None:
    async def scenario() -> None:
        receiver = Receiver(
            [
                input_command({"prompt": "", "id": "prompt", "type": "prompt"}),
                input_command({"id": "shutdown", "type": "shutdown"}),
                _RpcCommandCompleted("prompt", "prompt", True, (), 1),
                input_command({"id": "cancel", "type": "cancel", "target_id": "shutdown"}),
                _RpcInputClosed(),
            ]
        )
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        dispatched: list[str] = []

        async def dispatch(
            command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command.command_id)
            dispatched.append(command_id)
            if command.command_type == "prompt":
                return _RpcDispatchResult(
                    _RpcRunningCommand(command_id, "prompt", anyio.CancelScope())
                )
            if command.command_type == "cancel":
                assert coordinator.cancel("shutdown").outcome == "queued"
            return _RpcDispatchResult(running)

        assert (
            await coordinator.run(
                receiver,
                dispatch=dispatch,
                reject=ignore_reject,
            )
        ) is False
        assert dispatched == ["prompt", "cancel"]

    anyio.run(scenario)


def test_coordinator_drains_buffered_cancel_before_queued_shutdown_async() -> None:
    async def scenario() -> None:
        receiver = Receiver(
            [
                input_command({"prompt": "", "id": "prompt", "type": "prompt"}),
                input_command({"id": "shutdown", "type": "shutdown"}),
                _RpcCommandCompleted("prompt", "prompt", True, (), 1),
                input_command({"id": "cancel", "type": "cancel", "target_id": "shutdown"}),
                _RpcInputClosed(),
            ]
        )
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        dispatched: list[str] = []

        async def dispatch(
            command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command.command_id)
            dispatched.append(command_id)
            if command.command_type == "prompt":
                return _RpcDispatchResult(
                    _RpcRunningCommand(command_id, "prompt", anyio.CancelScope())
                )
            if command.command_type == "cancel":
                assert coordinator.cancel("shutdown").outcome == "queued"
            return _RpcDispatchResult(running)

        async def reject(_command: ParsedRpcCommand, _message: str) -> None:
            return None

        assert (
            await coordinator.run(
                receiver,
                dispatch=dispatch,
                reject=reject,
            )
        ) is False
        assert dispatched == ["prompt", "cancel"]

    anyio.run(scenario)


def test_coordinator_drains_buffered_control_before_idle_shutdown_async() -> None:
    async def scenario() -> None:
        receiver = Receiver(
            [
                input_command({"id": "shutdown", "type": "shutdown"}),
                input_command({"id": "cancel", "type": "cancel", "target_id": "missing"}),
            ]
        )
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        dispatched: list[str] = []

        async def dispatch(
            command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command.command_id)
            dispatched.append(command_id)
            return _RpcDispatchResult(
                running,
                should_shutdown=command.command_type == "shutdown",
            )

        async def reject(_command: ParsedRpcCommand, _message: str) -> None:
            return None

        assert (
            await coordinator.run(
                receiver,
                dispatch=dispatch,
                reject=reject,
            )
        ) is True
        assert dispatched == ["cancel", "shutdown"]

    anyio.run(scenario)


def test_coordinator_rejects_buffered_work_before_idle_shutdown_dispatch() -> None:
    async def scenario() -> None:
        receiver = Receiver(
            [
                input_command({"id": "shutdown", "type": "shutdown"}),
                input_command({"prompt": "", "id": "unreached", "type": "prompt"}),
            ]
        )
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        rejected: list[tuple[ParsedRpcCommand, str]] = []

        async def dispatch(
            _command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            return _RpcDispatchResult(running, should_shutdown=True)

        async def reject(command: ParsedRpcCommand, message: str) -> None:
            rejected.append((command, message))

        should_shutdown = await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=reject,
        )

        assert should_shutdown is True
        assert not receiver.events
        assert list(rejected) == expected_rejections(
            [
                (
                    {"prompt": "", "id": "unreached", "type": "prompt"},
                    "RPC command rejected because shutdown is pending",
                )
            ]
        )

    anyio.run(scenario)
