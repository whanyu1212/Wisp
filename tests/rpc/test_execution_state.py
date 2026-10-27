from __future__ import annotations

from pathlib import Path
from typing import Literal

import anyio
import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

from tests.rpc.execution_support import (
    dispatch_parsed,
    parsed_command,
)
from tests.support.rpc import (
    build_rpc_executor_fixture,
    guard_rpc_command_serialization,
)
from wisp.agent.harness import QueuedMessages
from wisp.coding.session import _RetainedQueueState
from wisp.events import (
    ErrorEvent,
    RpcCommandFinished,
    RpcCommandStarted,
    RpcStateReported,
    RpcStateSnapshot,
)
from wisp.messages import Message
from wisp.rpc.commands import ParsedRpcCommand
from wisp.rpc.coordinator import _RpcRunningCommand


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)


@pytest.mark.parametrize("command_type", ["prompt", "compact", "get_session_stats"])
def test_executor_reports_state_without_replacing_running_command(
    tmp_path: Path,
    command_type: Literal["prompt", "compact", "get_session_stats"],
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected_session = fixture.sessions.create()
        fixture.session_state.session = selected_session
        scope = anyio.CancelScope()
        running = _RpcRunningCommand("active-1", command_type, scope)
        if command_type == "prompt":
            scope.cancel()
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await dispatch_parsed(
                executor, {"id": "state-1", "type": "get_state"}, running
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is running
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcStateReported,
            RpcCommandFinished,
        ]
        report = fixture.events[1]
        assert isinstance(report, RpcStateReported)
        assert report.state == RpcStateSnapshot(
            provider="fake",
            model="fake",
            effort=None,
            auto_compaction_enabled=True,
            steering_mode="one_at_a_time",
            follow_up_mode="one_at_a_time",
            pending_steering_count=0,
            pending_follow_up_count=0,
            session_id=selected_session.session_id,
            session_path=selected_session.path,
            active_command_id="active-1",
            active_command_type=command_type,
            cancel_requested=command_type == "prompt",
        )

    anyio.run(scenario)


def test_executor_rejects_runtime_configuration_while_busy(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        running = _RpcRunningCommand("active-1", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await executor.dispatch_parsed(
                parsed_command(
                    {
                        "id": "configure-1",
                        "type": "configure",
                        "auto_compaction_enabled": False,
                    }
                ),
                running,
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is running
        finished = next(event for event in fixture.events if isinstance(event, RpcCommandFinished))
        assert finished.ok is False
        assert finished.error == "Cannot configure while another RPC operation is active"
        assert fixture.agent.auto_compaction_enabled is True

    anyio.run(scenario)


def test_executor_new_session_resets_selected_state_and_rejects_while_busy(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()
        await selected.append_message(Message(role="user", content="previous"))
        fixture.session_state.session = selected
        fixture.session_state.history = (Message(role="user", content="previous"),)
        fixture.session_state.entry_count = 1
        fixture.session_state.name = "Previous"
        fixture.agent._last_session_id = selected.session_id  # noqa: SLF001
        fixture.agent._retained_queues[selected.session_id] = _RetainedQueueState(  # noqa: SLF001
            messages=QueuedMessages(follow_up=(Message(role="user", content="stale follow-up"),))
        )
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            busy = _RpcRunningCommand("active-1", "prompt", anyio.CancelScope())

            rejected = await dispatch_parsed(
                executor,
                {"id": "new-busy", "type": "new_session"},
                busy,
            )
            accepted = await dispatch_parsed(
                executor,
                {"id": "new-1", "type": "new_session"},
            )
            assert rejected.reset_session is False
            assert accepted.reset_session is True
            fixture.coordinator._reset_session_state()
            task_group.cancel_scope.cancel()

        assert fixture.session_state.session is None
        assert fixture.session_state.history == ()
        assert fixture.session_state.entry_count == 0
        assert fixture.session_state.name is None
        assert fixture.agent.state_snapshot().pending_follow_up_count == 0
        assert fixture.agent._last_session_id is None  # noqa: SLF001
        assert not fixture.agent._retained_queues  # noqa: SLF001
        assert selected.path.is_file()
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_id, event.ok, event.error) for event in finished] == [
            (
                "new-busy",
                False,
                "Cannot start a new session while another RPC operation is active",
            ),
            ("new-1", True, None),
        ]

    anyio.run(scenario)


def test_executor_configures_agent_mode_and_reports_it_in_state(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            await executor.dispatch_parsed(
                parsed_command({"id": "configure-1", "type": "configure", "mode": "plan"}),
                None,
            )
            await dispatch_parsed(executor, {"id": "state-1", "type": "get_state"}, None)
            task_group.cancel_scope.cancel()

        assert fixture.agent.mode == "plan"
        report = next(event for event in fixture.events if isinstance(event, RpcStateReported))
        assert report.state.mode == "plan"

    anyio.run(scenario)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("mode", "RPC configure command field mode must be 'build' or 'plan'"),
        (
            "auto_compaction_enabled",
            "RPC configure command field auto_compaction_enabled must be a boolean",
        ),
    ],
)
def test_executor_preserves_explicit_null_configure_rejections(
    tmp_path: Path,
    field: str,
    message: str,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            await executor.dispatch_parsed(
                parsed_command(
                    {
                        "id": "configure-1",
                        "type": "configure",
                        "model": "custom-model",
                        field: None,
                    }
                ),
                None,
            )
            task_group.cancel_scope.cancel()

        assert fixture.agent.model is None
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
        ]
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.command_id == "configure-1"
        assert finished.ok is False
        assert finished.error == message

    anyio.run(scenario)


def test_executor_state_projects_prompt_startup_queue_buffer(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected_session = fixture.sessions.create()
        fixture.session_state.session = selected_session
        buffered_commands: list[dict[str, object]] = [
            {"id": [], "type": "steer", "content": "not real"},
            {
                "id": "",
                "type": "set_queue_mode",
                "kind": "follow_up",
                "mode": "all",
            },
            {"type": "steer", "content": "anonymous"},
            {"id": "steer-1", "type": "steer", "content": "redirect"},
            {"id": "follow-1", "type": "follow_up", "content": "continue"},
            {
                "id": "mode",
                "type": "set_queue_mode",
                "kind": "steering",
                "mode": "all",
            },
            {"id": "steer-2", "type": "steer", "content": "refine"},
            {"id": "pop", "type": "pop_queue", "kind": "steering"},
            {"id": "clear", "type": "clear_queue", "kind": "follow_up"},
            {"id": "invalid", "type": "follow_up"},
        ]
        parsed_commands = []
        for payload in buffered_commands:
            try:
                parsed_commands.append(parsed_command(payload))
            except ValidationError:
                # Deliberately injected internal corruption, never accepted at ingress.
                parsed_commands.append(ParsedRpcCommand.from_unknown(payload))
        fixture.coordinator.pending_prompt_queue_commands.extend(parsed_commands)
        running = _RpcRunningCommand("prompt", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await dispatch_parsed(
                executor, {"id": "state-1", "type": "get_state"}, running
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is running
        report = next(event for event in fixture.events if isinstance(event, RpcStateReported))
        assert report.state.pending_steering_count == 2
        assert report.state.pending_follow_up_count == 0
        assert report.state.steering_mode == "all"
        assert report.state.follow_up_mode == "one_at_a_time"
        assert list(fixture.coordinator.pending_prompt_queue_commands) == parsed_commands

    anyio.run(scenario)


def test_executor_state_is_idle_safe_and_reports_snapshot_failures(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            idle = await dispatch_parsed(executor, {"id": "idle", "type": "get_state"}, None)

            def fail_snapshot(_session: object = None) -> object:
                raise RuntimeError("snapshot failed")

            monkeypatch.setattr(fixture.agent, "state_snapshot", fail_snapshot)
            failed = await dispatch_parsed(executor, {"id": "failed", "type": "get_state"}, None)
            task_group.cancel_scope.cancel()

        assert idle.running_command is None
        assert failed.running_command is None
        report = next(event for event in fixture.events if isinstance(event, RpcStateReported))
        assert report.state.session_id is None
        assert report.state.session_path is None
        assert report.state.active_command_id is None
        assert report.state.active_command_type is None
        assert report.state.cancel_requested is False
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_type, event.ok, event.error) for event in finished] == [
            ("get_state", True, None),
            ("get_state", False, "snapshot failed"),
        ]

    anyio.run(scenario)
