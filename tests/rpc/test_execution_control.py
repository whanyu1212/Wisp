from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from tests.rpc.execution_support import (
    ApprovalResolver,
    TrustResolver,
    dispatch_parsed,
    parsed_command,
)
from tests.support.rpc import (
    build_rpc_executor_fixture,
    guard_rpc_command_serialization,
    reject_unexpected_command,
)
from wisp.coding import CodingSession
from wisp.config.runtime import WispConfig
from wisp.events import (
    RpcCommandFinished,
    RpcCommandStarted,
    WispEvent,
)
from wisp.rpc.commands import (
    ApprovalCommand,
    CancelCommand,
    ParsedRpcCommand,
    RpcCommandAdapter,
    TrustCommand,
)
from wisp.rpc.configuration import _RpcConfigureOverrides
from wisp.rpc.coordinator import (
    RpcCoordinator,
    _RpcRunningCommand,
    _RpcSessionState,
)
from wisp.rpc.execution import RpcCommandExecutor
from wisp.rpc.handlers import control as rpc_control_module
from wisp.rpc.host import RpcHost
from wisp.runtime.extensions import build_runtime
from wisp.sessions.jsonl import JsonlSessionStore


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)


def test_approval_resolution_waits_for_lifecycle_flush() -> None:
    events: list[WispEvent] = []
    deferred: list[Callable[[], None]] = []
    resolved: list[dict[str, object]] = []

    class PendingApproval:
        def has_pending_approval(self, *, call_id: str) -> bool:
            return call_id == "call-1"

        def resolve_approval(self, **kwargs: object) -> bool:
            resolved.append(kwargs)
            return True

    rpc_control_module.handle_rpc_approval_command(
        ApprovalCommand(id="approval-1", call_id="call-1", approved=True),
        command_id="approval-1",
        command_type="approval",
        approval_policy=PendingApproval(),
        write_event=events.append,
        defer_resolution=deferred.append,
    )

    assert len(events) == 1
    assert isinstance(events[0], RpcCommandFinished)
    assert events[0].command_id == "approval-1"
    assert events[0].ok is True
    assert not resolved
    assert len(deferred) == 1

    deferred[0]()

    assert resolved == [
        {
            "call_id": "call-1",
            "approved": True,
            "reason": None,
            "scope": "once",
        }
    ]


def test_approval_resolution_runs_without_post_flush_callback() -> None:
    events: list[WispEvent] = []
    resolved: list[dict[str, object]] = []

    class PendingApproval:
        def has_pending_approval(self, *, call_id: str) -> bool:
            return call_id == "call-1"

        def resolve_approval(self, **kwargs: object) -> bool:
            resolved.append(kwargs)
            return True

    rpc_control_module.handle_rpc_approval_command(
        ApprovalCommand(id="approval-1", call_id="call-1", approved=True),
        command_id="approval-1",
        command_type="approval",
        approval_policy=PendingApproval(),
        write_event=events.append,
    )

    assert resolved == [
        {
            "call_id": "call-1",
            "approved": True,
            "reason": None,
            "scope": "once",
        }
    ]
    assert len(events) == 1
    assert isinstance(events[0], RpcCommandFinished)
    assert events[0].ok is True


def test_active_cancellation_waits_for_lifecycle_flush() -> None:
    events: list[WispEvent] = []
    deferred: list[Callable[[], None]] = []

    class RecordingCancelScope:
        cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    cancel_scope = RecordingCancelScope()
    running_command = _RpcRunningCommand(
        command_id="prompt-1",
        command_type="prompt",
        cancel_scope=cancel_scope,
    )
    rpc_control_module.handle_rpc_cancel_command(
        CancelCommand(id="cancel-1", target_id="prompt-1"),
        command_id="cancel-1",
        command_type="cancel",
        running_command=running_command,
        write_event=events.append,
        defer_cancellation=deferred.append,
    )

    assert len(events) == 1
    assert isinstance(events[0], RpcCommandFinished)
    assert events[0].command_id == "cancel-1"
    assert events[0].ok is True
    assert cancel_scope.cancelled is False
    assert len(deferred) == 1

    deferred[0]()

    assert cancel_scope.cancelled is True


@pytest.mark.parametrize("buffered", [False, True])
def test_executor_routes_queued_cancellation_through_coordinator(
    tmp_path: Path, buffered: bool
) -> None:
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
        queued = parsed_command(
            {"id": "queued", "type": "steer", "content": "text"}
            if buffered
            else {"id": "queued", "type": "prompt", "prompt": "text"}
        )
        queue = (
            coordinator.pending_prompt_queue_commands if buffered else coordinator.queued_commands
        )
        await coordinator._enqueue_command(queued, queue=queue, reject=reject_unexpected_command)
        assert coordinator._queued_command_bytes == queued.payload_size
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
                approval_policy=ApprovalResolver(),
                trust_gate=TrustResolver(),
                configure_overrides=_RpcConfigureOverrides(),
                coordinator=coordinator,
                write_event=events.append,
                render_events=render_events,
            )

            result = await dispatch_parsed(
                executor, {"id": "cancel", "type": "cancel", "target_id": "queued"}, None
            )

            assert result.running_command is None
            assert not queue
            assert coordinator._queued_command_bytes == 0
            assert coordinator._duplicate_outstanding_id(queued) is None
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
                approval_policy=ApprovalResolver(),
                trust_gate=TrustResolver(),
                configure_overrides=_RpcConfigureOverrides(),
                coordinator=coordinator,
                write_event=events.append,
                render_events=render_events,
            )

            await dispatch_parsed(
                executor, {"id": "cancel", "type": "cancel", "target_id": "active"}, running
            )
            task_group.cancel_scope.cancel()

        assert active_scope.cancel_called is True
        assert coordinator.running_command is running
        finished = [event for event in events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_id, event.ok) for event in finished] == [("cancel", True)]

    anyio.run(scenario)


@pytest.mark.parametrize("id_fields", [{}, {"id": None}, {"id": "control-1"}])
@pytest.mark.parametrize(
    "payload",
    [
        {"type": "cancel", "target_id": "active"},
        {"type": "approval", "call_id": "call", "approved": True},
        {"type": "trust", "request_id": "request", "trusted": True},
        {"type": "shutdown"},
    ],
)
def test_control_commands_stay_typed_and_correlated(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    id_fields: dict[str, object],
    payload: dict[str, object],
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        effects: list[str] = []
        deferred: list[Callable[[], None]] = []

        class Approval:
            def has_pending_approval(self, **_kwargs: object) -> bool:
                return True

            def resolve_approval(self, **_kwargs: object) -> bool:
                effects.append("approval")
                return True

        class Trust:
            def resolve_request(self, **kwargs: object) -> bool:
                assert kwargs["release"] is False
                effects.append("decision")
                return True

            def release_request(self, **_kwargs: object) -> None:
                effects.append("release")

        running = _RpcRunningCommand("active", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            executor.approval_policy = Approval()
            executor.trust_gate = Trust()
            executor.defer_until_after_flush = deferred.append
            # Validate directly: invalid known payloads must never become unknown envelopes.
            command = RpcCommandAdapter.validate_python({**payload, **id_fields})
            result = await executor.dispatch_parsed(ParsedRpcCommand.from_known(command), running)
            assert result.running_command is running
            assert fixture.coordinator.running_command is running
            assert result.should_shutdown is (payload["type"] == "shutdown")
            assert [type(event) for event in fixture.events] == [
                RpcCommandStarted,
                RpcCommandFinished,
            ]
            first, last = fixture.events
            assert first.command_id == last.command_id
            assert first.command_id
            assert first.command_type == last.command_type == payload["type"]
            assert last.ok is True
            if id_fields.get("id") is not None:
                assert first.command_id == id_fields["id"]
            assert effects == (["decision"] if payload["type"] == "trust" else [])
            assert running.cancel_scope.cancel_called is False
            assert len(deferred) == (0 if payload["type"] == "shutdown" else 1)
            for callback in deferred:
                callback()
            assert effects == {"trust": ["decision", "release"], "approval": ["approval"]}.get(
                payload["type"], []
            )
            assert running.cancel_scope.cancel_called is (payload["type"] == "cancel")
            task_group.cancel_scope.cancel()

    anyio.run(scenario)


@pytest.mark.parametrize(
    "fields",
    [
        {"approved": True},
        {"approved": False},
        {"approved": True, "scope": None},
        {"approved": False, "scope": None},
        *[{"approved": True, "scope": scope} for scope in ("once", "tool_session", "all_session")],
    ],
)
@pytest.mark.parametrize("deferred_mode", [False, True])
def test_typed_approval_defaults_and_resolution(
    fields: dict[str, object], deferred_mode: bool
) -> None:
    command = ApprovalCommand.model_validate({"call_id": "call", "reason": " reason ", **fields})
    events: list[WispEvent] = []
    deferred: list[Callable[[], None]] = []
    decisions: list[dict[str, object]] = []

    class Approval:
        def has_pending_approval(self, **_kwargs: object) -> bool:
            return True

        def resolve_approval(self, **kwargs: object) -> bool:
            decisions.append(kwargs)
            return True

    rpc_control_module.handle_rpc_control_command(
        command,
        running_command=None,
        approval_policy=Approval(),
        write_event=events.append,
        defer_until_after_flush=deferred.append if deferred_mode else None,
    )
    assert bool(decisions) is not deferred_mode
    for callback in deferred:
        callback()
    assert decisions == [
        {
            "call_id": "call",
            "approved": fields["approved"],
            "reason": " reason ",
            "scope": fields.get("scope") or "once",
        }
    ]
    assert events[-1].ok is True


@pytest.mark.parametrize(
    "pending,resolve_ok,deferred_mode",
    [(False, True, False), (True, False, False), (True, False, True)],
)
def test_typed_approval_missing_or_lost_decision(
    pending: bool, resolve_ok: bool, deferred_mode: bool
) -> None:
    events: list[WispEvent] = []
    deferred: list[Callable[[], None]] = []
    resolutions: list[bool] = []

    class Approval:
        def has_pending_approval(self, **_kwargs: object) -> bool:
            return pending

        def resolve_approval(self, **_kwargs: object) -> bool:
            resolutions.append(True)
            return resolve_ok

    rpc_control_module.handle_rpc_control_command(
        ApprovalCommand(call_id="missing", approved=True),
        running_command=None,
        approval_policy=Approval(),
        write_event=events.append,
        defer_until_after_flush=deferred.append if deferred_mode else None,
    )
    before = list(events)
    for callback in deferred:
        callback()
    assert events == before
    assert events[-1].ok is deferred_mode
    assert resolutions == ([True] if pending else [])
    if not deferred_mode:
        assert events[-1].error == "No pending tool approval with call_id: missing"


@pytest.mark.parametrize(
    "transient_fields", [{}, {"transient": None}, {"transient": False}, {"transient": True}]
)
@pytest.mark.parametrize("trusted", [False, True])
@pytest.mark.parametrize("deferred_mode", [False, True])
def test_typed_trust_decision_and_release_order(
    transient_fields: dict[str, object], trusted: bool, deferred_mode: bool
) -> None:
    trace: list[object] = []
    deferred: list[Callable[[], None]] = []

    class Trust:
        def resolve_request(self, **kwargs: object) -> bool:
            trace.append(kwargs)
            return True

        def release_request(self, **kwargs: object) -> None:
            trace.append(kwargs)

    command = TrustCommand.model_validate(
        {"request_id": "request", "trusted": trusted, "reason": " reason ", **transient_fields}
    )
    rpc_control_module.handle_rpc_control_command(
        command,
        running_command=None,
        approval_policy=ApprovalResolver(),
        trust_gate=Trust(),
        write_event=trace.append,
        defer_until_after_flush=deferred.append if deferred_mode else None,
    )
    assert isinstance(trace[0], RpcCommandStarted)
    assert trace[1] == {
        "request_id": "request",
        "trusted": trusted,
        "reason": " reason ",
        "transient": transient_fields.get("transient") is True,
        "release": not deferred_mode,
    }
    assert isinstance(trace[2], RpcCommandFinished) and trace[2].ok
    assert len(trace) == 3
    for callback in deferred:
        callback()
    if deferred_mode:
        assert trace[3:] == [{"request_id": "request"}]


@pytest.mark.parametrize("has_gate", [False, True])
def test_typed_trust_rejects_missing_gate_or_request(has_gate: bool) -> None:
    events: list[WispEvent] = []
    deferred: list[Callable[[], None]] = []
    rpc_control_module.handle_rpc_control_command(
        TrustCommand(request_id="missing", trusted=True),
        running_command=None,
        approval_policy=ApprovalResolver(),
        trust_gate=TrustResolver() if has_gate else None,
        write_event=events.append,
        defer_until_after_flush=deferred.append,
    )
    assert not deferred
    assert events[-1].ok is False
    assert events[-1].error == (
        "No pending trust request with request_id: missing"
        if has_gate
        else "RPC trust command requires an active trust gate"
    )


def test_typed_cancellation_requires_coordinator_without_deferred_active_target() -> None:
    with pytest.raises(RuntimeError, match="requires the shared coordinator"):
        rpc_control_module.handle_rpc_control_command(
            CancelCommand(target_id="missing"),
            running_command=None,
            approval_policy=ApprovalResolver(),
            write_event=lambda _event: None,
        )


@pytest.mark.parametrize("command_type", ["cancel", "approval", "trust"])
@pytest.mark.parametrize("fail_flush", [False, True])
def test_control_host_flush_gates_side_effects(
    tmp_path: Path, monkeypatch: MonkeyPatch, command_type: str, fail_flush: bool
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        effects: list[str] = []
        rendered: list[WispEvent] = []
        running = _RpcRunningCommand("active", "prompt", anyio.CancelScope())

        class Approval:
            def has_pending_approval(self, **_kwargs: object) -> bool:
                return True

            def resolve_approval(self, **_kwargs: object) -> bool:
                effects.append("approval")
                return True

        class Trust:
            def resolve_request(self, **kwargs: object) -> bool:
                assert kwargs["release"] is False
                effects.append("decision")
                return True

            def release_request(self, **_kwargs: object) -> None:
                effects.append("release")

        async def render(events: AsyncIterator[WispEvent]) -> None:
            async for event in events:
                rendered.append(event)
                await anyio.lowlevel.checkpoint()
                assert effects == (["decision"] if command_type == "trust" else [])
                assert not running.cancel_scope.cancel_called
                if isinstance(event, RpcCommandFinished) and fail_flush:
                    raise RuntimeError("flush failed")

        payload = {
            "cancel": {"type": "cancel", "target_id": "active"},
            "approval": {"type": "approval", "call_id": "call", "approved": True},
            "trust": {"type": "trust", "request_id": "request", "trusted": True},
        }[command_type]

        async def run(_receive: object, *, dispatch: object, reject: object) -> bool:
            await dispatch(
                ParsedRpcCommand.from_known(RpcCommandAdapter.validate_python(payload)), running
            )
            return False

        monkeypatch.setattr(fixture.coordinator, "run", run)
        host = RpcHost(
            runtime=fixture.runtime,
            sessions=fixture.sessions,
            agent=fixture.agent,
            approval_policy=Approval(),
            trust_gate=Trust(),
            configure_overrides=_RpcConfigureOverrides(),
            coordinator=fixture.coordinator,
            write_event=fixture.events.append,
            render_events=render,
        )
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            if fail_flush:
                with pytest.raises(RuntimeError, match="flush failed"):
                    await host.run_with_streams(receive, send=send, task_group=task_group)
            else:
                await host.run_with_streams(receive, send=send, task_group=task_group)
            task_group.cancel_scope.cancel()
        assert [type(event) for event in rendered] == [RpcCommandStarted, RpcCommandFinished]
        assert running.cancel_scope.cancel_called is (command_type == "cancel" and not fail_flush)
        assert effects == (
            (["decision"] if fail_flush else ["decision", "release"])
            if command_type == "trust"
            else (["approval"] if command_type == "approval" and not fail_flush else [])
        )

    anyio.run(scenario)
