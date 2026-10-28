from __future__ import annotations

import anyio
import pytest

from tests.rpc.coordinator_support import (
    Receiver,
    expected_commands,
    expected_rejections,
    ignore_reject,
    input_command,
    parsed_command,
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


def test_coordinator_runs_message_read_alongside_active_prompt() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        prompt = _RpcRunningCommand("prompt", "prompt", anyio.CancelScope())
        coordinator.running_command = prompt
        message_read = _RpcRunningCommand("messages", "get_messages", anyio.CancelScope())

        async def dispatch(
            command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            assert command.command_type == "get_messages"
            assert running is prompt
            return _RpcDispatchResult(message_read)

        await coordinator.handle_event(
            input_command(
                {
                    "id": "messages",
                    "type": "get_messages",
                    "allow_during_prompt": True,
                }
            ),
            dispatch=dispatch,
            reject=ignore_reject,
        )

        assert coordinator.running_command is prompt
        assert coordinator.auxiliary_commands == {"messages": message_read}

        await coordinator.handle_event(
            _RpcCommandCompleted("messages", "get_messages", True, (), 0),
            dispatch=dispatch,
            reject=ignore_reject,
        )

        assert coordinator.running_command is prompt
        assert coordinator.auxiliary_commands == {}

    anyio.run(scenario)


def test_coordinator_bounds_concurrent_message_reads_during_a_prompt() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(
            _RpcSessionState(None, (), 0),
            max_queued_commands=2,
        )
        prompt = _RpcRunningCommand("prompt", "prompt", anyio.CancelScope())
        coordinator.running_command = prompt
        dispatched: list[str] = []
        rejected: list[tuple[str, str]] = []

        async def dispatch(
            command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            assert running is prompt
            command_id = str(command.command_id)
            dispatched.append(command_id)
            return _RpcDispatchResult(
                _RpcRunningCommand(command_id, "get_messages", anyio.CancelScope())
            )

        async def reject(command: ParsedRpcCommand, message: str) -> None:
            rejected.append((str(command.command_id), message))

        for index in range(3):
            await coordinator.handle_event(
                input_command(
                    {
                        "id": f"messages-{index}",
                        "type": "get_messages",
                        "allow_during_prompt": True,
                    }
                ),
                dispatch=dispatch,
                reject=reject,
            )

        assert coordinator.running_command is prompt
        assert dispatched == ["messages-0", "messages-1"]
        assert set(coordinator.auxiliary_commands) == {"messages-0", "messages-1"}
        assert rejected == [
            (
                "messages-2",
                "RPC command queue is full while another RPC command is running",
            )
        ]

    anyio.run(scenario)


def test_coordinator_bounds_concurrent_message_read_bytes_during_a_prompt() -> None:
    async def scenario() -> None:
        first = {
            "id": "messages-0",
            "type": "get_messages",
            "allow_during_prompt": True,
            "entry_ids": ["x" * 64],
        }
        second = {
            "id": "messages-1",
            "type": "get_messages",
            "allow_during_prompt": True,
            "entry_ids": ["y" * 64],
        }
        coordinator = RpcCoordinator(
            _RpcSessionState(None, (), 0),
            max_queued_bytes=RpcCoordinator._command_payload_size(parsed_command(first)),
        )
        prompt = _RpcRunningCommand("prompt", "prompt", anyio.CancelScope())
        coordinator.running_command = prompt
        dispatched: list[str] = []
        rejected: list[tuple[str, str]] = []

        async def dispatch(
            command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            assert running is prompt
            command_id = str(command.command_id)
            dispatched.append(command_id)
            return _RpcDispatchResult(
                _RpcRunningCommand(command_id, "get_messages", anyio.CancelScope())
            )

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

        assert dispatched == ["messages-0"]
        assert rejected == [
            (
                "messages-1",
                "RPC command queue byte limit exceeded while another RPC command is running",
            )
        ]

        await coordinator.handle_event(
            _RpcCommandCompleted("messages-0", "get_messages", True, (), 0),
            dispatch=dispatch,
            reject=reject,
        )
        await coordinator.handle_event(
            input_command(second),
            dispatch=dispatch,
            reject=reject,
        )
        assert dispatched == ["messages-0", "messages-1"]

    anyio.run(scenario)


def test_coordinator_bounds_shutdown_queued_behind_an_auxiliary_read() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(
            _RpcSessionState(None, (), 0),
            max_queued_commands=1,
        )
        prompt = _RpcRunningCommand("prompt", "prompt", anyio.CancelScope())
        coordinator.running_command = prompt
        message_read = _RpcRunningCommand("messages", "get_messages", anyio.CancelScope())
        rejected: list[tuple[ParsedRpcCommand, str]] = []

        async def dispatch(
            command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            assert command.command_type == "get_messages"
            assert running is prompt
            return _RpcDispatchResult(message_read)

        async def reject(command: ParsedRpcCommand, message: str) -> None:
            rejected.append((command, message))

        await coordinator.handle_event(
            input_command(
                {
                    "id": "messages",
                    "type": "get_messages",
                    "allow_during_prompt": True,
                }
            ),
            dispatch=dispatch,
            reject=reject,
        )
        await coordinator.handle_event(
            _RpcCommandCompleted("prompt", "prompt", True, (), 0),
            dispatch=dispatch,
            reject=reject,
        )

        shutdown = {"id": "shutdown", "type": "shutdown"}
        await coordinator.handle_event(
            input_command(shutdown),
            dispatch=dispatch,
            reject=reject,
        )

        assert coordinator.running_command is None
        assert coordinator.auxiliary_commands == {"messages": message_read}
        assert not coordinator.queued_commands
        assert list(rejected) == expected_rejections(
            [(shutdown, "RPC command queue is full while another RPC command is running")]
        )

    anyio.run(scenario)


def test_coordinator_preserves_fifo_order_while_auxiliary_read_finishes() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        auxiliary = _RpcRunningCommand("messages", "get_messages", anyio.CancelScope())
        coordinator.auxiliary_commands[auxiliary.command_id] = auxiliary
        coordinator.queued_commands.append(
            parsed_command({"provider": "fake", "id": "configure", "type": "configure"})
        )
        dispatched: list[str] = []

        async def dispatch(
            command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            assert running is None
            dispatched.append(str(command.command_id))
            return _RpcDispatchResult(
                _RpcRunningCommand(
                    str(command.command_id),
                    command.command_type,
                    anyio.CancelScope(),
                )
            )

        await coordinator.handle_event(
            input_command({"prompt": "", "id": "later-prompt", "type": "prompt"}),
            dispatch=dispatch,
            reject=ignore_reject,
        )

        assert dispatched == []
        assert list(coordinator.queued_commands) == expected_commands(
            [
                {"provider": "fake", "id": "configure", "type": "configure"},
                {"prompt": "", "id": "later-prompt", "type": "prompt"},
            ]
        )

        receiver = Receiver(
            [
                _RpcCommandCompleted("messages", "get_messages", True, (), 0),
                _RpcCommandCompleted("configure", "configure", True, (), 0),
                _RpcCommandCompleted("later-prompt", "prompt", True, (), 0),
                _RpcInputClosed(),
            ]
        )
        await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=ignore_reject,
        )

        assert dispatched == ["configure", "later-prompt"]

    anyio.run(scenario)


@pytest.mark.parametrize("command_type", ["new_session", "select_session"])
def test_coordinator_queues_session_work_behind_auxiliary_read(command_type: str) -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        auxiliary = _RpcRunningCommand("messages", "get_messages", anyio.CancelScope())
        coordinator.auxiliary_commands[auxiliary.command_id] = auxiliary
        dispatched: list[str] = []

        await coordinator.handle_event(
            input_command(
                {
                    "id": "session-work",
                    "type": command_type,
                    **({"session_id": "session-1"} if command_type == "select_session" else {}),
                }
            ),
            dispatch=lambda command, running: (
                dispatched.append(str(command.command_id)) or _RpcDispatchResult(running)
            ),
            reject=ignore_reject,
        )

        assert dispatched == []
        assert list(coordinator.queued_commands) == expected_commands(
            [
                {
                    "id": "session-work",
                    "type": command_type,
                    **({"session_id": "session-1"} if command_type == "select_session" else {}),
                }
            ]
        )

    anyio.run(scenario)


def test_discovery_does_not_remove_history_read_session_ordering_barrier() -> None:
    async def scenario() -> None:
        receiver = Receiver(
            [
                input_command({"type": "prompt", "id": "prompt", "prompt": "hello"}),
                input_command({"type": "get_project_files", "id": "files"}),
                input_command(
                    {"type": "get_messages", "id": "history", "allow_during_prompt": True}
                ),
                input_command({"type": "select_session", "id": "select", "session_id": "next"}),
                _RpcCommandCompleted("prompt", "prompt", True, (), 1),
                _RpcCommandCompleted("history", "get_messages", True, None, 0),
                _RpcCommandCompleted("select", "select_session", True, (), 5),
                _RpcCommandCompleted("files", "get_project_files", True, None, 0),
                _RpcInputClosed(),
            ]
        )
        state = _RpcSessionState(session=None, history=(), entry_count=0)
        coordinator = RpcCoordinator(state)
        dispatched: list[str] = []

        async def dispatch(
            command: ParsedRpcCommand, running: _RpcRunningCommand | None
        ) -> _RpcDispatchResult:
            command_id = str(command.command_id)
            dispatched.append(command_id)
            if command_id in {"files", "history"}:
                assert running is not None and running.command_id == "prompt"
            elif command_id == "select":
                assert running is None
                # The history read has settled, but file discovery is still alive.
                assert set(coordinator.auxiliary_commands) == {"files"}
            return _RpcDispatchResult(
                _RpcRunningCommand(command_id, command.command_type, anyio.CancelScope())
            )

        assert not await coordinator.run(receiver, dispatch=dispatch, reject=ignore_reject)
        assert dispatched == ["prompt", "files", "history", "select"]
        assert state.entry_count == 5
        assert not coordinator.auxiliary_commands

    anyio.run(scenario)


def test_only_shutdown_cancels_running_discovery() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        discovery = _RpcRunningCommand("files", "get_project_files", anyio.CancelScope())
        coordinator.auxiliary_commands["files"] = discovery

        async def dispatch(
            _command: ParsedRpcCommand, running: _RpcRunningCommand | None
        ) -> _RpcDispatchResult:
            return _RpcDispatchResult(running)

        await coordinator.handle_event(
            input_command({"id": "state", "type": "get_state"}),
            dispatch=dispatch,
            reject=ignore_reject,
        )
        assert not discovery.cancel_scope.cancel_called

        await coordinator.handle_event(
            input_command({"id": "stop", "type": "shutdown"}),
            dispatch=dispatch,
            reject=ignore_reject,
        )
        assert discovery.cancel_scope.cancel_called

    anyio.run(scenario)


def test_read_that_finishes_during_dispatch_releases_its_bytes() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        prompt = _RpcRunningCommand("run", "prompt", anyio.CancelScope())
        coordinator.running_command = prompt

        async def dispatch(
            _command: ParsedRpcCommand, _running: _RpcRunningCommand | None
        ) -> _RpcDispatchResult:
            return _RpcDispatchResult(None)

        should_shutdown = await coordinator.handle_event(
            input_command({"id": "files", "type": "get_project_files"}),
            dispatch=dispatch,
            reject=ignore_reject,
        )

        assert should_shutdown is False
        assert coordinator.running_command is prompt
        assert not coordinator.auxiliary_commands
        assert coordinator._queued_command_bytes == 0  # noqa: SLF001

    anyio.run(scenario)
