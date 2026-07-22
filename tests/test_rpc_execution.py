from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import anyio

from tests.rpc_support import build_rpc_executor_fixture
from wisp.cli.rpc_configuration import _RpcConfigureOverrides
from wisp.cli.rpc_coordinator import RpcCoordinator, _RpcRunningCommand, _RpcSessionState
from wisp.cli.rpc_execution import RpcCommandExecutor
from wisp.coding import CodingSession
from wisp.config import WispConfig
from wisp.events import ErrorEvent, RpcCommandFinished, RpcCommandStarted, WispEvent
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
