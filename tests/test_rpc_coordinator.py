from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import anyio
import pytest
from pydantic import ValidationError

from wisp.agent.messages import Message
from wisp.events import RpcSessionSelected, WispEvent
from wisp.rpc.commands import ParsedRpcCommand, RpcCommandAdapter, StoreApiKeyCommand
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


def _parsed_command(payload: dict[str, object]) -> ParsedRpcCommand:
    try:
        command = RpcCommandAdapter.validate_json(json.dumps(payload))
    except ValidationError as exc:
        if isinstance(payload.get("type"), str) and any(
            error["type"] == "union_tag_invalid" for error in exc.errors(include_input=False)
        ):
            return ParsedRpcCommand.from_unknown(payload)
        raise
    return ParsedRpcCommand.from_known(command, payload=payload)


def _input_command(payload: dict[str, object]) -> _RpcInputCommand:
    return _RpcInputCommand(_parsed_command(payload))


def _expected_commands(payloads: Iterable[dict[str, object]]) -> list[ParsedRpcCommand]:
    return [_parsed_command(payload) for payload in payloads]


def _expected_rejections(
    pairs: Iterable[tuple[dict[str, object], str]],
) -> list[tuple[ParsedRpcCommand, str]]:
    return [(_parsed_command(payload), message) for payload, message in pairs]


async def _ignore_reject(_command: ParsedRpcCommand, _message: str) -> None:
    return None


def test_coordinator_runs_queued_commands_in_fifo_order() -> None:
    async def scenario() -> None:
        history = (Message(role="user", content="done"),)
        receiver = _Receiver(
            [
                _input_command({"prompt": "", "id": "one", "type": "prompt"}),
                _input_command({"prompt": "", "id": "two", "type": "prompt"}),
                _input_command({"prompt": "", "id": "three", "type": "prompt"}),
                _RpcCommandCompleted("one", "prompt", True, history, 1),
                _RpcCommandCompleted("two", "prompt", True, history, 2),
                _RpcCommandCompleted("three", "prompt", True, history, 3),
                _RpcInputClosed(),
            ]
        )
        state = _RpcSessionState(session=None, history=(), entry_count=0)
        coordinator = RpcCoordinator(state)
        dispatched: list[str] = []

        async def dispatch(
            command: ParsedRpcCommand,
            _running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command.command_id)
            dispatched.append(command_id)
            return _RpcDispatchResult(_RpcRunningCommand(command_id, "prompt", anyio.CancelScope()))

        should_shutdown = await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=_ignore_reject,
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
                _input_command({"id": "new-1", "type": "new_session"}),
                _RpcInputClosed(),
            ]
        )

        async def dispatch(
            _command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            return _RpcDispatchResult(running_command=running, reset_session=True)

        await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=_ignore_reject,
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
        coordinator.queued_commands.append(
            _parsed_command({"prompt": "", "id": "prompt", "type": "prompt"})
        )
        dispatched: list[str] = []

        await coordinator.handle_event(
            _input_command({"id": "new", "type": "new_session"}),
            dispatch=lambda command, running: (
                dispatched.append(str(command.command_id)) or _RpcDispatchResult(running)
            ),
            reject=_ignore_reject,
        )

        assert dispatched == []
        assert list(coordinator.queued_commands) == _expected_commands(
            [{"prompt": "", "id": "prompt", "type": "prompt"}, {"id": "new", "type": "new_session"}]
        )
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

        await coordinator.handle_event(
            _input_command({"id": "new", "type": "new_session"}),
            dispatch=lambda command, running: (
                dispatched.append(str(command.command_id)) or _RpcDispatchResult(running)
            ),
            reject=_ignore_reject,
        )

        assert dispatched == []
        assert list(coordinator.queued_commands) == _expected_commands(
            [{"id": "new", "type": "new_session"}]
        )
        assert background.cancel_scope.cancel_called is False

    anyio.run(scenario)


def test_coordinator_queues_new_session_behind_work_waiting_on_stats_async() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        stats = _RpcRunningCommand("stats", "get_session_stats", anyio.CancelScope())
        coordinator.running_command = stats
        coordinator.queued_commands.append(
            _parsed_command({"prompt": "", "id": "prompt", "type": "prompt"})
        )
        dispatched: list[str] = []

        async def dispatch(
            command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            dispatched.append(str(command.command_id))
            return _RpcDispatchResult(running)

        async def reject(_command: ParsedRpcCommand, _message: str) -> None:
            raise AssertionError("new_session should be queued, not rejected")

        await coordinator.handle_event(
            _input_command({"id": "new", "type": "new_session"}),
            dispatch=dispatch,
            reject=reject,
        )

        assert dispatched == []
        assert list(coordinator.queued_commands) == _expected_commands(
            [{"prompt": "", "id": "prompt", "type": "prompt"}, {"id": "new", "type": "new_session"}]
        )
        assert stats.cancel_scope.cancel_called is False

    anyio.run(scenario)


def test_coordinator_dispatches_control_commands_while_active() -> None:
    async def scenario() -> None:
        receiver = _Receiver(
            [
                _input_command({"prompt": "", "id": "prompt", "type": "prompt"}),
                _input_command({"prompt": "", "id": "queued", "type": "prompt"}),
                _input_command(
                    {
                        "id": "messages",
                        "type": "get_messages",
                        "allow_during_prompt": True,
                    }
                ),
                _input_command({"id": "sessions", "type": "get_sessions"}),
                _input_command(
                    {"session_id": "session-1", "id": "select", "type": "select_session"}
                ),
                _input_command({"id": "clone", "type": "clone_session"}),
                _input_command({"id": "fork", "type": "fork_session", "entry_id": "entry"}),
                _input_command({"id": "tree", "type": "get_session_tree"}),
                _input_command(
                    {"id": "navigate", "type": "navigate_session_tree", "entry_id": "entry"}
                ),
                _input_command({"id": "unrevert", "type": "unrevert_session_tree"}),
                _input_command({"id": "commands", "type": "get_commands"}),
                _input_command(
                    {"call_id": "call-1", "approved": True, "id": "approval", "type": "approval"}
                ),
                _input_command({"content": "", "id": "steer", "type": "steer"}),
                _input_command({"content": "", "id": "follow", "type": "follow_up"}),
                _input_command({"id": "state", "type": "get_queue_state"}),
                _input_command(
                    {"kind": "steering", "mode": "all", "id": "mode", "type": "set_queue_mode"}
                ),
                _input_command({"kind": "follow_up", "id": "pop", "type": "pop_queue"}),
                _input_command({"id": "clear", "type": "clear_queue"}),
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
            reject=_ignore_reject,
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


@pytest.mark.parametrize(
    ("active_type", "completed_ok"),
    [
        ("prompt", True),
        ("get_session_stats", True),
        ("prompt", False),
    ],
)
def test_coordinator_orders_configure_between_active_work_and_later_prompt(
    active_type: Literal["prompt", "get_session_stats"],
    completed_ok: bool,
) -> None:
    async def scenario() -> None:
        receiver = _Receiver(
            [
                _input_command(
                    {
                        "id": "active",
                        "type": active_type,
                        **({"prompt": ""} if active_type == "prompt" else {}),
                    }
                ),
                _input_command({"provider": "fake", "id": "configure", "type": "configure"}),
                _input_command({"prompt": "", "id": "prompt-after", "type": "prompt"}),
                _RpcCommandCompleted("active", active_type, completed_ok, (), 0),
                _RpcCommandCompleted("prompt-after", "prompt", True, (), 1),
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
            command_type = command.command_type
            dispatched.append(command_id)
            if command_type == "configure":
                assert running is None
                return _RpcDispatchResult(None)
            return _RpcDispatchResult(
                _RpcRunningCommand(command_id, command_type, anyio.CancelScope())
            )

        async def reject(_command: ParsedRpcCommand, _message: str) -> None:
            raise AssertionError("ordered configure should be queued, not rejected")

        await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=reject,
        )

        assert dispatched == ["active", "configure", "prompt-after"]
        assert not coordinator.queued_commands

    anyio.run(scenario)


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
            _input_command(
                {"id": "run", "type": run_type, **({"prompt": ""} if run_type == "prompt" else {})}
            ),
            dispatch=dispatch,
            reject=_ignore_reject,
        )
        await coordinator.handle_event(
            _input_command({"content": "", "id": "steer", "type": "steer"}),
            dispatch=dispatch,
            reject=_ignore_reject,
        )

        assert dispatched == ["run"]
        assert list(coordinator.pending_prompt_queue_commands) == _expected_commands(
            [{"content": "", "id": "steer", "type": "steer"}]
        )

        await coordinator.handle_event(
            _RpcPromptReady("run"),
            dispatch=dispatch,
            reject=_ignore_reject,
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
            command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            dispatched.append(str(command.command_id))
            return _RpcDispatchResult(running)

        async def reject(_command: ParsedRpcCommand, _message: str) -> None:
            raise AssertionError("command unexpectedly rejected")

        await coordinator.handle_event(
            _input_command({"content": "", "id": "follow-up", "type": "follow_up"}),
            dispatch=dispatch,
            reject=reject,
        )
        assert dispatched == []

        await coordinator.handle_event(
            _RpcPromptReady("init"),
            dispatch=dispatch,
            reject=reject,
        )
        assert dispatched == ["follow-up"]

    anyio.run(scenario)


def test_coordinator_dispatches_new_session_while_init_is_running() -> None:
    async def scenario() -> None:
        sync_coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        sync_coordinator.running_command = _RpcRunningCommand("init", "init", anyio.CancelScope())
        sync_dispatched: list[str] = []

        async def sync_dispatch(
            command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            sync_dispatched.append(str(command.command_id))
            return _RpcDispatchResult(running)

        await sync_coordinator.handle_event(
            _input_command({"id": "new-sync", "type": "new_session"}),
            dispatch=sync_dispatch,
            reject=_ignore_reject,
        )
        assert sync_dispatched == ["new-sync"]
        assert not sync_coordinator.queued_commands

        async_coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        async_coordinator.running_command = _RpcRunningCommand("init", "init", anyio.CancelScope())
        async_dispatched: list[str] = []

        async def async_dispatch(
            command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            async_dispatched.append(str(command.command_id))
            return _RpcDispatchResult(running)

        async def reject(_command: ParsedRpcCommand, _message: str) -> None:
            raise AssertionError("command unexpectedly rejected")

        await async_coordinator.handle_event(
            _input_command({"id": "new-async", "type": "new_session"}),
            dispatch=async_dispatch,
            reject=reject,
        )
        assert async_dispatched == ["new-async"]
        assert not async_coordinator.queued_commands

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
                _input_command(command),
                dispatch=dispatch,
                reject=_ignore_reject,
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
        assert list(coordinator.pending_prompt_queue_commands) == _expected_commands(
            [{"content": "", "id": "steer", "type": "steer"}]
        )
        assert list(coordinator.queued_commands) == _expected_commands(
            [{"id": "queued", "type": "compact"}]
        )

        await coordinator.handle_event(
            _RpcPromptReady("prompt"),
            dispatch=dispatch,
            reject=_ignore_reject,
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
        assert list(coordinator.queued_commands) == _expected_commands(
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
                _input_command({"id": "state", "type": "get_state"}),
                dispatch=dispatch,
                reject=_ignore_reject,
            )
            await coordinator.handle_event(
                _input_command({"id": "commands", "type": "get_commands"}),
                dispatch=dispatch,
                reject=_ignore_reject,
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
            _input_command(
                {
                    "id": "messages",
                    "type": "get_messages",
                    "allow_during_prompt": True,
                }
            ),
            dispatch=dispatch,
            reject=_ignore_reject,
        )

        assert coordinator.running_command is prompt
        assert coordinator.auxiliary_commands == {"messages": message_read}

        await coordinator.handle_event(
            _RpcCommandCompleted("messages", "get_messages", True, (), 0),
            dispatch=dispatch,
            reject=_ignore_reject,
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
                _input_command(
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
            max_queued_bytes=RpcCoordinator._command_payload_size(_parsed_command(first)),
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
            _input_command(first),
            dispatch=dispatch,
            reject=reject,
        )
        await coordinator.handle_event(
            _input_command(second),
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
            _input_command(second),
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
            _input_command(
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
            _input_command(shutdown),
            dispatch=dispatch,
            reject=reject,
        )

        assert coordinator.running_command is None
        assert coordinator.auxiliary_commands == {"messages": message_read}
        assert not coordinator.queued_commands
        assert list(rejected) == _expected_rejections(
            [(shutdown, "RPC command queue is full while another RPC command is running")]
        )

    anyio.run(scenario)


def test_coordinator_preserves_fifo_order_while_auxiliary_read_finishes() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        auxiliary = _RpcRunningCommand("messages", "get_messages", anyio.CancelScope())
        coordinator.auxiliary_commands[auxiliary.command_id] = auxiliary
        coordinator.queued_commands.append(
            _parsed_command({"provider": "fake", "id": "configure", "type": "configure"})
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
            _input_command({"prompt": "", "id": "later-prompt", "type": "prompt"}),
            dispatch=dispatch,
            reject=_ignore_reject,
        )

        assert dispatched == []
        assert list(coordinator.queued_commands) == _expected_commands(
            [
                {"provider": "fake", "id": "configure", "type": "configure"},
                {"prompt": "", "id": "later-prompt", "type": "prompt"},
            ]
        )

        receiver = _Receiver(
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
            reject=_ignore_reject,
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
            _input_command(
                {
                    "id": "session-work",
                    "type": command_type,
                    **({"session_id": "session-1"} if command_type == "select_session" else {}),
                }
            ),
            dispatch=lambda command, running: (
                dispatched.append(str(command.command_id)) or _RpcDispatchResult(running)
            ),
            reject=_ignore_reject,
        )

        assert dispatched == []
        assert list(coordinator.queued_commands) == _expected_commands(
            [
                {
                    "id": "session-work",
                    "type": command_type,
                    **({"session_id": "session-1"} if command_type == "select_session" else {}),
                }
            ]
        )

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

        await coordinator.handle_event(
            _RpcCommandCompleted("clone", "clone_session", True, history, 1, session),
            dispatch=lambda _command, running: _RpcDispatchResult(running),
            reject=_ignore_reject,
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
            reject=_ignore_reject,
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
            reject=_ignore_reject,
        )

        assert coordinator.running_command is None
        assert state.session is previous

    anyio.run(scenario)


def test_coordinator_ignores_stale_prompt_readiness() -> None:
    async def scenario() -> None:
        receiver = _Receiver(
            [
                _input_command({"prompt": "", "id": "prompt", "type": "prompt"}),
                _input_command({"content": "", "id": "steer", "type": "steer"}),
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
            reject=_ignore_reject,
        )

        assert dispatched == [("prompt", None), ("steer", None)]

    anyio.run(scenario)


def test_coordinator_does_not_retarget_queue_commands_when_prompt_fails_before_ready() -> None:
    async def scenario() -> None:
        receiver = _Receiver(
            [
                _input_command({"prompt": "", "id": "first", "type": "prompt"}),
                _input_command({"prompt": "", "id": "second", "type": "prompt"}),
                _input_command({"content": "", "id": "steer", "type": "steer"}),
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
            reject=_ignore_reject,
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
                _input_command({"prompt": "", "id": "same", "type": "prompt"}),
                _input_command({"prompt": "", "id": "same", "type": "prompt"}),
                _RpcCommandCompleted("same", "prompt", True, (), 1),
                _input_command({"prompt": "", "id": "same", "type": "prompt"}),
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
        assert list(rejected) == _expected_rejections(
            [({"prompt": "", "type": "prompt"}, "RPC command id is already outstanding: same")]
        )
        assert coordinator.session_state.entry_count == 2

    anyio.run(scenario)


def test_coordinator_rejects_duplicate_ids_in_both_queues() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        coordinator.running_command = _RpcRunningCommand("active", "prompt", anyio.CancelScope())
        coordinator.pending_prompt_queue_commands.append(
            _parsed_command({"id": "pending", "type": "steer", "content": "redirect"})
        )
        coordinator.queued_commands.append(
            _parsed_command({"id": "queued", "type": "prompt", "prompt": "later"})
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
                _input_command({"id": command_id, "type": "prompt", "prompt": "duplicate"}),
                dispatch=dispatch,
                reject=reject,
            )

        assert list(rejected) == _expected_rejections(
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
            _input_command({"id": "same", "type": "get_state"}),
            dispatch=dispatch,
            reject=reject,
        )

        assert coordinator.running_command is running
        assert list(rejected) == _expected_rejections(
            [({"type": "get_state"}, "RPC command id is already outstanding: same")]
        )

    anyio.run(scenario)


def test_coordinator_rejects_commands_beyond_its_queue_bound() -> None:
    async def scenario() -> None:
        receiver = _Receiver(
            [
                _input_command({"prompt": "", "id": "active", "type": "prompt"}),
                _input_command({"prompt": "", "id": "queued", "type": "prompt"}),
                _input_command({"prompt": "", "id": "overflow", "type": "prompt"}),
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
            _input_command(first),
            dispatch=dispatch,
            reject=reject,
        )
        await coordinator.handle_event(
            _input_command(second),
            dispatch=dispatch,
            reject=reject,
        )

        assert list(coordinator.queued_commands) == _expected_commands([first])
        assert rejected == [
            (
                "queued-2",
                "RPC command queue byte limit exceeded while another RPC command is running",
            )
        ]

        assert coordinator.cancel("queued-1").outcome == "queued"
        await coordinator.handle_event(
            _input_command(second),
            dispatch=dispatch,
            reject=reject,
        )
        assert list(coordinator.queued_commands) == _expected_commands([second])

    anyio.run(scenario)


def test_coordinator_ignores_stale_completion_and_closes_decisions_once() -> None:
    async def scenario() -> None:
        closed = 0

        def on_closed() -> None:
            nonlocal closed
            closed += 1

        receiver = _Receiver(
            [
                _input_command({"prompt": "", "id": "active", "type": "prompt"}),
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

        async def dispatch(
            command: ParsedRpcCommand,
            _running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            return _RpcDispatchResult(
                _RpcRunningCommand(str(command.command_id), "prompt", anyio.CancelScope())
            )

        await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=_ignore_reject,
        )

        assert closed == 1
        assert coordinator.session_state.entry_count == 1

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
            reject=_ignore_reject,
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
            _input_command(device_code),
            dispatch=lambda _command, running: _RpcDispatchResult(running),
            reject=reject,
        )
        assert list(coordinator.queued_commands) == _expected_commands([device_code])

        await coordinator.handle_event(
            _RpcInputClosed(),
            dispatch=lambda _command, running: _RpcDispatchResult(running),
            reject=reject,
        )

        assert list(coordinator.queued_commands) == _expected_commands([])
        assert coordinator._queued_command_bytes == 0
        assert list(rejected) == _expected_rejections(
            [(device_code, "RPC command cancelled: device-code-1")]
        )

    anyio.run(scenario)


def test_coordinator_drains_buffered_cancel_before_queued_shutdown() -> None:
    async def scenario() -> None:
        receiver = _Receiver(
            [
                _input_command({"prompt": "", "id": "prompt", "type": "prompt"}),
                _input_command({"id": "shutdown", "type": "shutdown"}),
                _RpcCommandCompleted("prompt", "prompt", True, (), 1),
                _input_command({"id": "cancel", "type": "cancel", "target_id": "shutdown"}),
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
                reject=_ignore_reject,
            )
        ) is False
        assert dispatched == ["prompt", "cancel"]

    anyio.run(scenario)


def test_coordinator_drains_buffered_cancel_before_queued_shutdown_async() -> None:
    async def scenario() -> None:
        receiver = _Receiver(
            [
                _input_command({"prompt": "", "id": "prompt", "type": "prompt"}),
                _input_command({"id": "shutdown", "type": "shutdown"}),
                _RpcCommandCompleted("prompt", "prompt", True, (), 1),
                _input_command({"id": "cancel", "type": "cancel", "target_id": "shutdown"}),
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
        coordinator.queued_commands.append(_parsed_command({"id": "next", "type": "get_sessions"}))

        async def dispatch(
            command: ParsedRpcCommand,
            _running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            next_dispatched.set()
            return _RpcDispatchResult(
                _RpcRunningCommand(
                    str(command.command_id),
                    "get_sessions",
                    anyio.CancelScope(),
                )
            )

        async def reject(_command: ParsedRpcCommand, _message: str) -> None:
            return None

        async def run_coordinator() -> None:
            await coordinator.run(
                receiver,
                dispatch=dispatch,
                reject=reject,
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
                _input_command({"id": "shutdown", "type": "shutdown"}),
                _input_command({"id": "cancel", "type": "cancel", "target_id": "missing"}),
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
        receiver = _Receiver(
            [
                _input_command({"id": "shutdown", "type": "shutdown"}),
                _input_command({"prompt": "", "id": "unreached", "type": "prompt"}),
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
        assert list(rejected) == _expected_rejections(
            [
                (
                    {"prompt": "", "id": "unreached", "type": "prompt"},
                    "RPC command rejected because shutdown is pending",
                )
            ]
        )

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
                _input_command({"prompt": "", "id": "active", "type": "prompt"}),
                CompatInputClosed(),
                CompatCommandCompleted("active", "prompt", True, (), 1),
            ]
        )
        coordinator = RpcCoordinator(
            _RpcSessionState(None, (), 0),
            input_closed_type=CompatInputClosed,
            command_completed_type=CompatCommandCompleted,
        )

        async def dispatch(
            command: ParsedRpcCommand,
            _running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            return _RpcDispatchResult(
                _RpcRunningCommand(str(command.command_id), "prompt", anyio.CancelScope())
            )

        await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=_ignore_reject,
        )

        assert coordinator.session_state.entry_count == 1

    anyio.run(scenario)


def test_coordinator_owns_running_and_queued_cancellation() -> None:
    async def scenario() -> None:
        state = _RpcSessionState(None, (), 0)
        coordinator = RpcCoordinator(state)
        active_scope = anyio.CancelScope()
        coordinator.running_command = _RpcRunningCommand("active", "prompt", active_scope)
        pending = _parsed_command({"content": "", "id": "pending", "type": "steer"})
        coordinator.pending_prompt_queue_commands.append(pending)
        queued = _parsed_command({"prompt": "", "id": "queued", "type": "prompt"})
        coordinator.queued_commands.extend(
            [queued, _parsed_command({"prompt": "", "id": "later", "type": "prompt"})]
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
        assert list(coordinator.queued_commands) == _expected_commands(
            [{"prompt": "", "id": "later", "type": "prompt"}]
        )
        assert missing_result.outcome == "missing"

    anyio.run(scenario)
