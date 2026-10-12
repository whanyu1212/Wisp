from __future__ import annotations

from typing import Literal

import anyio

from tests.rpc.coordinator_support import (
    Receiver,
    expected_commands,
    ignore_reject,
    input_command,
    parsed_command,
)
from wisp.rpc.commands import ParsedRpcCommand, StoreApiKeyCommand
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


def test_coordinator_dispatches_control_commands_while_active() -> None:
    async def scenario() -> None:
        receiver = Receiver(
            [
                input_command({"prompt": "", "id": "prompt", "type": "prompt"}),
                input_command({"prompt": "", "id": "queued", "type": "prompt"}),
                input_command(
                    {
                        "id": "messages",
                        "type": "get_messages",
                        "allow_during_prompt": True,
                    }
                ),
                input_command({"id": "sessions", "type": "get_sessions"}),
                input_command(
                    {"session_id": "session-1", "id": "select", "type": "select_session"}
                ),
                input_command({"id": "clone", "type": "clone_session"}),
                input_command({"id": "fork", "type": "fork_session", "entry_id": "entry"}),
                input_command({"id": "tree", "type": "get_session_tree"}),
                input_command(
                    {"id": "navigate", "type": "navigate_session_tree", "entry_id": "entry"}
                ),
                input_command({"id": "unrevert", "type": "unrevert_session_tree"}),
                input_command({"id": "commands", "type": "get_commands"}),
                input_command(
                    {"call_id": "call-1", "approved": True, "id": "approval", "type": "approval"}
                ),
                input_command({"content": "", "id": "steer", "type": "steer"}),
                input_command({"content": "", "id": "follow", "type": "follow_up"}),
                input_command({"id": "state", "type": "get_queue_state"}),
                input_command(
                    {"kind": "steering", "mode": "all", "id": "mode", "type": "set_queue_mode"}
                ),
                input_command({"kind": "follow_up", "id": "pop", "type": "pop_queue"}),
                input_command({"id": "clear", "type": "clear_queue"}),
                _RpcPromptReady("prompt"),
                _RpcCommandCompleted("messages", "get_messages", True, (), 1),
                _RpcCommandCompleted("prompt", "prompt", True, (), 1),
                _RpcCommandCompleted("queued", "prompt", True, (), 2),
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

        async def dispatch(
            command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command.command_id)
            dispatched.append(command_id)
            if command.command_type not in {
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
                _RpcRunningCommand(command_id, command.command_type, anyio.CancelScope())
            )

        await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=ignore_reject,
        )

        assert dispatched == [
            "prompt",
            "messages",
            "commands",
            "approval",
            "steer",
            "follow",
            "state",
            "mode",
            "pop",
            "clear",
            "queued",
            "sessions",
            "select",
            "clone",
            "fork",
            "tree",
            "navigate",
            "unrevert",
        ]

    anyio.run(scenario)


def test_coordinator_state_bypasses_active_prompt_without_draining_pending_queue() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        dispatched: list[tuple[str, str | None]] = []

        async def dispatch(
            command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command.command_id)
            dispatched.append((command_id, running.command_id if running is not None else None))
            if command.command_type == "prompt":
                return _RpcDispatchResult(
                    _RpcRunningCommand(command_id, "prompt", anyio.CancelScope())
                )
            return _RpcDispatchResult(running)

        async def handle(command: ParsedRpcCommand) -> None:
            await coordinator.handle_event(
                input_command(command),
                dispatch=dispatch,
                reject=ignore_reject,
            )

        await handle({"prompt": "", "id": "prompt", "type": "prompt"})
        await handle({"content": "", "id": "steer", "type": "steer"})
        await handle({"id": "queued", "type": "compact"})
        await handle({"id": "state-before", "type": "get_state"})
        await handle({"id": "commands-before", "type": "get_commands"})

        assert dispatched == [
            ("prompt", None),
            ("state-before", "prompt"),
            ("commands-before", "prompt"),
        ]
        assert list(coordinator.pending_prompt_queue_commands) == expected_commands(
            [{"content": "", "id": "steer", "type": "steer"}]
        )
        assert list(coordinator.queued_commands) == expected_commands(
            [{"id": "queued", "type": "compact"}]
        )

        await coordinator.handle_event(
            _RpcPromptReady("prompt"),
            dispatch=dispatch,
            reject=ignore_reject,
        )
        await handle({"id": "state-after", "type": "get_state"})
        await handle({"id": "commands-after", "type": "get_commands"})

        assert dispatched == [
            ("prompt", None),
            ("state-before", "prompt"),
            ("commands-before", "prompt"),
            ("steer", "prompt"),
            ("state-after", "prompt"),
            ("commands-after", "prompt"),
        ]
        assert not coordinator.pending_prompt_queue_commands
        assert list(coordinator.queued_commands) == expected_commands(
            [{"id": "queued", "type": "compact"}]
        )
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

            async def dispatch(
                command: ParsedRpcCommand,
                running: _RpcRunningCommand | None,
            ) -> _RpcDispatchResult:
                dispatched.append(
                    (
                        str(command.command_id),
                        running.command_id if running is not None else None,
                    )
                )
                return _RpcDispatchResult(running)

            await coordinator.handle_event(
                input_command({"id": "state", "type": "get_state"}),
                dispatch=dispatch,
                reject=ignore_reject,
            )
            await coordinator.handle_event(
                input_command({"id": "commands", "type": "get_commands"}),
                dispatch=dispatch,
                reject=ignore_reject,
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


def test_secret_commands_and_cancel_results_have_redacted_reprs() -> None:
    secret = "sentinel-secret"
    command = ParsedRpcCommand.from_known(
        StoreApiKeyCommand(
            id="store-1",
            provider="anthropic",
            api_key=secret,
        )
    )
    coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
    coordinator.queued_commands.append(command)

    result = coordinator.cancel("store-1")

    assert secret not in repr(_RpcInputCommand(command))
    assert secret not in repr(command)
    assert secret not in repr(result)


def test_coordinator_owns_running_and_queued_cancellation() -> None:
    async def scenario() -> None:
        state = _RpcSessionState(None, (), 0)
        coordinator = RpcCoordinator(state)
        active_scope = anyio.CancelScope()
        coordinator.running_command = _RpcRunningCommand("active", "prompt", active_scope)
        pending = parsed_command({"content": "", "id": "pending", "type": "steer"})
        coordinator.pending_prompt_queue_commands.append(pending)
        queued = parsed_command({"prompt": "", "id": "queued", "type": "prompt"})
        coordinator.queued_commands.extend(
            [queued, parsed_command({"prompt": "", "id": "later", "type": "prompt"})]
        )

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
        assert list(coordinator.queued_commands) == expected_commands(
            [{"prompt": "", "id": "later", "type": "prompt"}]
        )
        assert missing_result.outcome == "missing"

    anyio.run(scenario)
