from __future__ import annotations

from collections.abc import Callable

import anyio
import pytest

from tests.rpc.coordinator_support import (
    Receiver,
    expected_commands,
    expected_rejections,
    input_command,
    parsed_command,
)
from wisp.coding.session import CodingSession
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


def test_coordinator_rejects_duplicate_running_and_allows_reuse_after_completion() -> None:
    async def scenario() -> None:
        receiver = Receiver(
            [
                input_command({"prompt": "", "id": "same", "type": "prompt"}),
                input_command({"prompt": "", "id": "same", "type": "prompt"}),
                _RpcCommandCompleted("same", "prompt", True, (), 1),
                input_command({"prompt": "", "id": "same", "type": "prompt"}),
                _RpcCommandCompleted("same", "prompt", True, (), 2),
                _RpcInputClosed(),
            ]
        )
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        dispatched: list[str] = []
        rejected: list[tuple[ParsedRpcCommand, str]] = []

        async def dispatch(
            command: ParsedRpcCommand,
            _running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command.command_id)
            dispatched.append(command_id)
            return _RpcDispatchResult(_RpcRunningCommand(command_id, "prompt", anyio.CancelScope()))

        async def reject(command: ParsedRpcCommand, message: str) -> None:
            rejected.append((command, message))

        await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=reject,
        )

        assert dispatched == ["same", "same"]
        assert list(rejected) == expected_rejections(
            [({"prompt": "", "type": "prompt"}, "RPC command id is already outstanding: same")]
        )
        assert coordinator.session_state.entry_count == 2

    anyio.run(scenario)


def test_coordinator_rejects_duplicate_ids_in_both_queues() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        coordinator.running_command = _RpcRunningCommand("active", "prompt", anyio.CancelScope())
        coordinator.pending_prompt_queue_commands.append(
            parsed_command({"id": "pending", "type": "steer", "content": "redirect"})
        )
        coordinator.queued_commands.append(
            parsed_command({"id": "queued", "type": "prompt", "prompt": "later"})
        )
        rejected: list[tuple[ParsedRpcCommand, str]] = []

        async def dispatch(
            _command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            return _RpcDispatchResult(running)

        async def reject(command: ParsedRpcCommand, message: str) -> None:
            rejected.append((command, message))

        for command_id in ("pending", "queued"):
            await coordinator.handle_event(
                input_command({"id": command_id, "type": "prompt", "prompt": "duplicate"}),
                dispatch=dispatch,
                reject=reject,
            )

        assert list(rejected) == expected_rejections(
            [
                (
                    {"type": "prompt", "prompt": "duplicate"},
                    "RPC command id is already outstanding: pending",
                ),
                (
                    {"type": "prompt", "prompt": "duplicate"},
                    "RPC command id is already outstanding: queued",
                ),
            ]
        )
        assert [command.command_id for command in coordinator.pending_prompt_queue_commands] == [
            "pending"
        ]
        assert [command.command_id for command in coordinator.queued_commands] == ["queued"]

    anyio.run(scenario)


def test_coordinator_rejects_duplicate_running_id_async() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        running = _RpcRunningCommand("same", "prompt", anyio.CancelScope())
        coordinator.running_command = running
        rejected: list[tuple[ParsedRpcCommand, str]] = []

        async def dispatch(
            _command: ParsedRpcCommand,
            _running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            raise AssertionError("duplicate command must not be dispatched")

        async def reject(command: ParsedRpcCommand, message: str) -> None:
            rejected.append((command, message))

        await coordinator.handle_event(
            input_command({"id": "same", "type": "get_state"}),
            dispatch=dispatch,
            reject=reject,
        )

        assert coordinator.running_command is running
        assert list(rejected) == expected_rejections(
            [({"type": "get_state"}, "RPC command id is already outstanding: same")]
        )

    anyio.run(scenario)


def test_coordinator_rejects_commands_beyond_its_queue_bound() -> None:
    async def scenario() -> None:
        receiver = Receiver(
            [
                input_command({"prompt": "", "id": "active", "type": "prompt"}),
                input_command({"prompt": "", "id": "queued", "type": "prompt"}),
                input_command({"prompt": "", "id": "overflow", "type": "prompt"}),
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

        async def dispatch(
            command: ParsedRpcCommand,
            _running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command.command_id)
            return _RpcDispatchResult(_RpcRunningCommand(command_id, "prompt", anyio.CancelScope()))

        async def reject(command: ParsedRpcCommand, message: str) -> None:
            rejected.append((str(command.command_id), message))

        await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=reject,
        )

        assert rejected == [
            ("overflow", "RPC command queue is full while another RPC command is running")
        ]

    anyio.run(scenario)


def test_coordinator_bounds_aggregate_queued_command_bytes() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(
            _RpcSessionState(None, (), 0),
            max_queued_bytes=128,
        )
        coordinator.running_command = _RpcRunningCommand("active", "prompt", anyio.CancelScope())
        first = {"id": "queued-1", "type": "prompt", "prompt": "x" * 64}
        second = {"id": "queued-2", "type": "prompt", "prompt": "y" * 64}
        rejected: list[tuple[str, str]] = []

        async def dispatch(
            _command: ParsedRpcCommand,
            _running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            raise AssertionError("queued commands must not dispatch while a prompt is active")

        async def reject(command: ParsedRpcCommand, message: str) -> None:
            rejected.append((str(command.command_id), message))

        await coordinator.handle_event(
            input_command(first),
            dispatch=dispatch,
            reject=reject,
        )
        await coordinator.handle_event(
            input_command(second),
            dispatch=dispatch,
            reject=reject,
        )

        assert list(coordinator.queued_commands) == expected_commands([first])
        assert rejected == [
            (
                "queued-2",
                "RPC command queue byte limit exceeded while another RPC command is running",
            )
        ]

        assert coordinator.cancel("queued-1").outcome == "queued"
        await coordinator.handle_event(
            input_command(second),
            dispatch=dispatch,
            reject=reject,
        )
        assert list(coordinator.queued_commands) == expected_commands([second])

    anyio.run(scenario)


def test_coordinator_rejects_held_messages_with_the_run_queue_error() -> None:
    """A steer beyond the run's limit gets the run's error even before the run is ready."""

    async def scenario() -> None:
        coordinator = RpcCoordinator(
            _RpcSessionState(None, (), 0),
            queued_message_rejection=CodingSession.queued_message_rejection,
        )
        prompt = _RpcRunningCommand("prompt", "prompt", anyio.CancelScope())
        coordinator.running_command = prompt
        # An outstanding read shares the command bound; it must not change which
        # error the 101st held steer gets.
        coordinator.auxiliary_commands["messages"] = _RpcRunningCommand(
            "messages", "get_messages", anyio.CancelScope()
        )
        dispatched: list[str] = []
        rejected: list[tuple[str, str]] = []

        async def dispatch(
            command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            dispatched.append(str(command.command_id))
            return _RpcDispatchResult(running)

        async def reject(command: ParsedRpcCommand, message: str) -> None:
            rejected.append((str(command.command_id), message))

        # Every steer arrives before the run accepts queued messages, so all are held.
        for index in range(101):
            await coordinator.handle_event(
                input_command({"id": f"steer-{index}", "type": "steer", "content": str(index)}),
                dispatch=dispatch,
                reject=reject,
            )
        await coordinator.handle_event(
            _RpcPromptReady("prompt"),
            dispatch=dispatch,
            reject=reject,
        )

        assert rejected == [("steer-100", "Agent queue is full (maximum 100 pending messages)")]
        assert dispatched == [f"steer-{index}" for index in range(100)]
        assert coordinator.running_command is prompt

    anyio.run(scenario)


def _prompt_running(coordinator: RpcCoordinator | None = None) -> RpcCoordinator:
    coordinator = coordinator or RpcCoordinator(_RpcSessionState(None, (), 0))
    coordinator.running_command = _RpcRunningCommand("run", "prompt", anyio.CancelScope())
    return coordinator


def _discovery_with_shutdown_queued() -> RpcCoordinator:
    coordinator = _prompt_running()
    coordinator.queued_commands.append(parsed_command({"id": "stop", "type": "shutdown"}))
    return coordinator


@pytest.mark.parametrize(
    ("make_coordinator", "payload", "message"),
    [
        (
            _discovery_with_shutdown_queued,
            {"id": "files", "type": "get_project_files"},
            "Project file discovery unavailable during shutdown",
        ),
        (
            lambda: _prompt_running(
                RpcCoordinator(
                    _RpcSessionState(None, (), 0), queued_message_rejection=lambda _held: "too many"
                )
            ),
            {"id": "steer", "type": "steer", "content": ""},
            "too many",
        ),
        (
            lambda: _prompt_running(
                RpcCoordinator(_RpcSessionState(None, (), 0), max_queued_commands=0)
            ),
            {"id": "files", "type": "get_project_files"},
            "RPC command queue is full while another RPC command is running",
        ),
        (
            lambda: _prompt_running(
                RpcCoordinator(_RpcSessionState(None, (), 0), max_queued_bytes=0)
            ),
            {"id": "files", "type": "get_project_files"},
            "RPC command queue byte limit exceeded while another RPC command is running",
        ),
    ],
    ids=["discovery-during-shutdown", "held-message-limit", "command-limit", "byte-limit"],
)
def test_rejected_command_does_not_stop_the_coordinator(
    make_coordinator: Callable[[], RpcCoordinator], payload: dict[str, object], message: str
) -> None:
    async def scenario() -> None:
        coordinator = make_coordinator()
        rejected: list[str] = []

        async def reject(_command: ParsedRpcCommand, reason: str) -> None:
            rejected.append(reason)

        should_shutdown = await coordinator.handle_event(
            input_command(payload), dispatch=_unexpected_dispatch, reject=reject
        )

        assert rejected == [message]
        assert should_shutdown is False

    anyio.run(scenario)


async def _unexpected_dispatch(
    command: ParsedRpcCommand, _running: _RpcRunningCommand | None
) -> _RpcDispatchResult:
    raise AssertionError(f"unexpected dispatch: {command.command_type}")
