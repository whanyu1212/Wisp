from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

import anyio
import pytest
from pytest import MonkeyPatch

from tests.rpc_support import build_rpc_executor_fixture
from wisp.agent.harness import QueuedMessages
from wisp.agent.messages import Message
from wisp.cli.rpc_configuration import _RpcConfigureOverrides
from wisp.cli.rpc_coordinator import RpcCoordinator, _RpcRunningCommand, _RpcSessionState
from wisp.cli.rpc_execution import RpcCommandExecutor
from wisp.coding import CodingSession
from wisp.config import WispConfig
from wisp.events import (
    ErrorEvent,
    QueueItemsRemoved,
    QueueUpdated,
    RpcCommandFinished,
    RpcCommandStarted,
    RpcStateReported,
    RpcStateSnapshot,
    WispEvent,
)
from wisp.runtime.extensions import build_runtime
from wisp.sessions.jsonl import JsonlSessionStore


class _ApprovalResolver:
    def resolve_approval(self, **_kwargs: object) -> bool:
        return False


class _TrustResolver:
    async def resolve(self) -> bool:
        return True

    def resolve_request(self, **_kwargs: object) -> bool:
        return False


def test_executor_dispatches_validation_and_shutdown_without_stdin(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            invalid = executor.dispatch({"id": "bad", "type": "prompt"}, None)
            shutdown = executor.dispatch({"id": "bye", "type": "shutdown"}, None)

            assert invalid.running_command is None
            assert invalid.should_shutdown is False
            assert shutdown.should_shutdown is True
            task_group.cancel_scope.cancel()

        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
            RpcCommandStarted,
            RpcCommandFinished,
        ]
        error_event = fixture.events[1]
        finished_event = fixture.events[-1]
        assert isinstance(error_event, ErrorEvent)
        assert isinstance(finished_event, RpcCommandFinished)
        assert error_event.message == "RPC prompt command requires string field: prompt"
        assert finished_event.command_id == "bye"

    anyio.run(scenario)


def test_executor_queue_state_is_idle_safe_and_mutations_fail_cleanly(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            state_result = executor.dispatch(
                {"id": "state", "type": "get_queue_state"},
                None,
            )
            mutation_commands: list[dict[str, object]] = [
                {"id": "steer", "type": "steer", "content": "redirect"},
                {"id": "follow", "type": "follow_up", "content": "continue"},
                {
                    "id": "mode",
                    "type": "set_queue_mode",
                    "kind": "steering",
                    "mode": "all",
                },
                {"id": "pop", "type": "pop_queue", "kind": "steering"},
                {"id": "clear", "type": "clear_queue"},
            ]
            results = [executor.dispatch(command, None) for command in mutation_commands]
            task_group.cancel_scope.cancel()

        assert state_result.running_command is None
        assert all(result.running_command is None for result in results)
        state_event = next(event for event in fixture.events if isinstance(event, QueueUpdated))
        assert state_event.steering == ()
        assert state_event.follow_up == ()
        assert state_event.steering_mode == "one_at_a_time"
        assert state_event.follow_up_mode == "one_at_a_time"
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_id, event.ok, event.error) for event in finished] == [
            ("state", True, None),
            *[
                (command_id, False, "CodingSession has no active agent run")
                for command_id in ("steer", "follow", "mode", "pop", "clear")
            ],
        ]

    anyio.run(scenario)


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

            result = executor.dispatch({"id": "state-1", "type": "get_state"}, running)
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


def test_executor_state_is_idle_safe_and_reports_snapshot_failures(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            idle = executor.dispatch({"id": "idle", "type": "get_state"}, None)
            malformed = executor.dispatch({"id": [], "type": "get_state"}, None)

            def fail_snapshot(_session: object = None) -> object:
                raise RuntimeError("snapshot failed")

            monkeypatch.setattr(fixture.agent, "state_snapshot", fail_snapshot)
            failed = executor.dispatch({"id": "failed", "type": "get_state"}, None)
            task_group.cancel_scope.cancel()

        assert idle.running_command is None
        assert malformed.running_command is None
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
            ("get_state", False, "RPC command id must be a non-empty string"),
            ("get_state", False, "snapshot failed"),
        ]

    anyio.run(scenario)


def test_executor_queue_commands_delegate_and_report_removed_items(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected_session = fixture.sessions.create()
        fixture.session_state.session = selected_session
        calls: list[tuple[object, ...]] = []

        def state(session: object = None) -> QueueUpdated:
            calls.append(("state", session))
            return QueueUpdated(steering=("one", "two"), follow_up=("later",))

        def steer(content: str) -> QueueUpdated:
            calls.append(("steer", content))
            return QueueUpdated(steering=(content,))

        def follow_up(content: str) -> QueueUpdated:
            calls.append(("follow_up", content))
            return QueueUpdated(follow_up=(content,))

        def set_mode(kind: object, mode: object) -> QueueUpdated:
            calls.append(("set_queue_mode", kind, mode))
            return QueueUpdated(steering_mode="all")

        def pop(kind: object) -> tuple[Message, QueueUpdated]:
            calls.append(("pop_queue", kind))
            return Message(role="user", content="two"), QueueUpdated(steering=("one",))

        def clear(kind: object = None) -> tuple[QueuedMessages, QueueUpdated]:
            calls.append(("clear_queue", kind))
            return (
                QueuedMessages(follow_up=(Message(role="user", content="later"),)),
                QueueUpdated(),
            )

        monkeypatch.setattr(fixture.agent, "queue_state", state)
        monkeypatch.setattr(fixture.agent, "steer", steer)
        monkeypatch.setattr(fixture.agent, "follow_up", follow_up)
        monkeypatch.setattr(fixture.agent, "set_queue_mode", set_mode)
        monkeypatch.setattr(fixture.agent, "pop_queue", pop)
        monkeypatch.setattr(fixture.agent, "clear_queue", clear)

        commands: list[dict[str, object]] = [
            {"id": "state", "type": "get_queue_state"},
            {"id": "steer", "type": "steer", "content": "redirect"},
            {"id": "follow", "type": "follow_up", "content": "continue"},
            {
                "id": "mode",
                "type": "set_queue_mode",
                "kind": "steering",
                "mode": "all",
            },
            {"id": "pop", "type": "pop_queue", "kind": "steering"},
            {"id": "clear", "type": "clear_queue", "kind": "follow_up"},
        ]
        running = _RpcRunningCommand("prompt", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            results = [executor.dispatch(command, running) for command in commands]
            task_group.cancel_scope.cancel()

        assert all(result.running_command is running for result in results)
        assert calls == [
            ("state", selected_session),
            ("steer", "redirect"),
            ("follow_up", "continue"),
            ("set_queue_mode", "steering", "all"),
            ("pop_queue", "steering"),
            ("clear_queue", "follow_up"),
        ]
        removed = [event for event in fixture.events if isinstance(event, QueueItemsRemoved)]
        assert [
            (
                event.command_id,
                event.operation,
                event.kind,
                event.steering,
                event.follow_up,
            )
            for event in removed
        ] == [
            ("pop", "pop", "steering", ("two",), ()),
            ("clear", "clear", "follow_up", (), ("later",)),
        ]
        assert all(event.ok for event in fixture.events if isinstance(event, RpcCommandFinished))

    anyio.run(scenario)


def test_executor_rejects_invalid_raw_queue_fields(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        commands: list[dict[str, object]] = [
            {"id": "steer", "type": "steer"},
            {"id": "mode-kind", "type": "set_queue_mode", "kind": "unknown", "mode": "all"},
            {
                "id": "mode-value",
                "type": "set_queue_mode",
                "kind": "steering",
                "mode": "invalid",
            },
            {"id": "pop", "type": "pop_queue"},
            {"id": "clear", "type": "clear_queue", "kind": "unknown"},
            {
                "id": "mode-kind-container",
                "type": "set_queue_mode",
                "kind": [],
                "mode": "all",
            },
            {
                "id": "mode-value-container",
                "type": "set_queue_mode",
                "kind": "steering",
                "mode": {},
            },
            {"id": "pop-container", "type": "pop_queue", "kind": []},
            {"id": "clear-container", "type": "clear_queue", "kind": {}},
        ]
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            for command in commands:
                executor.dispatch(command, None)
            executor.dispatch({"id": "state", "type": "get_queue_state"}, None)
            task_group.cancel_scope.cancel()

        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        errors = [event.error for event in finished if not event.ok]
        assert errors == [
            "RPC steer command requires string field: content",
            "RPC set_queue_mode command field kind must be 'steering' or 'follow_up'",
            "RPC set_queue_mode command field mode must be 'one_at_a_time' or 'all'",
            "RPC pop_queue command field kind must be 'steering' or 'follow_up'",
            "RPC clear_queue command field kind must be 'steering' or 'follow_up'",
            "RPC set_queue_mode command field kind must be 'steering' or 'follow_up'",
            "RPC set_queue_mode command field mode must be 'one_at_a_time' or 'all'",
            "RPC pop_queue command field kind must be 'steering' or 'follow_up'",
            "RPC clear_queue command field kind must be 'steering' or 'follow_up'",
        ]
        assert (finished[-1].command_id, finished[-1].ok) == ("state", True)

    anyio.run(scenario)


def test_executor_reports_empty_pop_and_clear_as_success(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        monkeypatch.setattr(
            fixture.agent,
            "pop_queue",
            lambda _kind: (None, QueueUpdated()),
        )
        monkeypatch.setattr(
            fixture.agent,
            "clear_queue",
            lambda _kind=None: (QueuedMessages(), QueueUpdated()),
        )
        running = _RpcRunningCommand("prompt", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            executor.dispatch(
                {"id": "pop", "type": "pop_queue", "kind": "steering"},
                running,
            )
            executor.dispatch({"id": "clear", "type": "clear_queue"}, running)
            task_group.cancel_scope.cancel()

        removed = [event for event in fixture.events if isinstance(event, QueueItemsRemoved)]
        assert [
            (event.command_id, event.kind, event.steering, event.follow_up) for event in removed
        ] == [
            ("pop", "steering", (), ()),
            ("clear", None, (), ()),
        ]
        assert [
            (event.command_id, event.ok)
            for event in fixture.events
            if isinstance(event, RpcCommandFinished)
        ] == [("pop", True), ("clear", True)]

    anyio.run(scenario)


def test_executor_routes_queued_cancellation_through_coordinator(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = WispConfig(provider="fake", session_dir=tmp_path)
        runtime = await build_runtime(
            auth_path=config.auth_path,
            retry_policy=config.retry_policy,
        )
        sessions = JsonlSessionStore(tmp_path)
        agent = CodingSession(provider=runtime.providers.get("fake"), sessions=sessions)
        state = _RpcSessionState(None, (), 0)
        coordinator = RpcCoordinator(state)
        coordinator.queued_commands.append({"id": "queued", "type": "prompt"})
        events: list[WispEvent] = []

        async def render_events(stream: AsyncIterator[WispEvent]) -> None:
            async for event in stream:
                events.append(event)

        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = RpcCommandExecutor(
                agent=agent,
                runtime=runtime,
                sessions=sessions,
                session_state=state,
                task_group=task_group,
                send=send,
                approval_policy=_ApprovalResolver(),
                trust_gate=_TrustResolver(),
                configure_overrides=_RpcConfigureOverrides(),
                coordinator=coordinator,
                write_event=events.append,
                render_events=render_events,
            )

            result = executor.dispatch(
                {"id": "cancel", "type": "cancel", "target_id": "queued"},
                None,
            )

            assert result.running_command is None
            assert not coordinator.queued_commands
            task_group.cancel_scope.cancel()

        finished = [event for event in events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_id, event.ok) for event in finished] == [
            ("queued", False),
            ("cancel", True),
        ]

    anyio.run(scenario)


def test_executor_synchronizes_running_command_before_cancellation(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = WispConfig(provider="fake", session_dir=tmp_path)
        runtime = await build_runtime(
            auth_path=config.auth_path,
            retry_policy=config.retry_policy,
        )
        sessions = JsonlSessionStore(tmp_path)
        agent = CodingSession(provider=runtime.providers.get("fake"), sessions=sessions)
        state = _RpcSessionState(None, (), 0)
        coordinator = RpcCoordinator(state)
        events: list[WispEvent] = []
        active_scope = anyio.CancelScope()
        running = _RpcRunningCommand("active", "prompt", active_scope)

        async def render_events(stream: AsyncIterator[WispEvent]) -> None:
            async for event in stream:
                events.append(event)

        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = RpcCommandExecutor(
                agent=agent,
                runtime=runtime,
                sessions=sessions,
                session_state=state,
                task_group=task_group,
                send=send,
                approval_policy=_ApprovalResolver(),
                trust_gate=_TrustResolver(),
                configure_overrides=_RpcConfigureOverrides(),
                coordinator=coordinator,
                write_event=events.append,
                render_events=render_events,
            )

            executor.dispatch(
                {"id": "cancel", "type": "cancel", "target_id": "active"},
                running,
            )
            task_group.cancel_scope.cancel()

        assert active_scope.cancel_called is True
        assert coordinator.running_command is running
        finished = [event for event in events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_id, event.ok) for event in finished] == [("cancel", True)]

    anyio.run(scenario)
