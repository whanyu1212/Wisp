from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError

from wisp.events import (
    ProjectConfigApplied,
    ProviderRetrying,
    QueueItemsRemoved,
    RpcCommandFinished,
    RpcCommandStarted,
    RpcMessageSnapshot,
    RpcMessagesReported,
    RpcMessageToolCallSnapshot,
    RpcStateReported,
    RpcStateSnapshot,
    TrustRequested,
    TrustResolved,
    wisp_event_from_json,
)
from wisp.rpc import (
    ClearQueueCommand,
    CompactCommand,
    ConfigureCommand,
    FollowUpCommand,
    GetMessagesCommand,
    GetQueueStateCommand,
    GetSessionStatsCommand,
    GetStateCommand,
    JsonlSubprocessRpcTransport,
    PopQueueCommand,
    RpcController,
    SetQueueModeCommand,
    SteerCommand,
)
from wisp.rpc.commands import (
    ApprovalCommand,
    CancelCommand,
    PromptCommand,
    RpcCommand,
    ShutdownCommand,
    TrustCommand,
    rpc_command_from_json,
)


class RecordingTransport:
    def __init__(self, events: list[object] | None = None) -> None:
        self.commands: list[RpcCommand] = []
        self._events = events or []
        self.closed = False

    async def send(self, command: RpcCommand) -> None:
        self.commands.append(command)

    async def close(self) -> None:
        self.closed = True

    def events(self) -> AsyncIterator[object]:
        return self._iter_events()

    async def _iter_events(self) -> AsyncIterator[object]:
        for event in self._events:
            yield event


def test_rpc_commands_serialize_as_jsonl_and_parse() -> None:
    command = ApprovalCommand(
        id="approval-1",
        call_id="call-1",
        approved=False,
        reason="not safe",
    )

    line = command.to_json_line()

    assert line.endswith("\n")
    assert json.loads(line) == {
        "id": "approval-1",
        "type": "approval",
        "call_id": "call-1",
        "approved": False,
        "reason": "not safe",
    }
    assert rpc_command_from_json(line) == command


@pytest.mark.parametrize("instructions", [None, "Keep exact paths"])
def test_compact_command_serializes_as_jsonl_and_parses(
    instructions: str | None,
) -> None:
    command = CompactCommand(id="compact-1", instructions=instructions)

    line = command.to_json_line()

    expected: dict[str, object] = {"id": "compact-1", "type": "compact"}
    if instructions is not None:
        expected["instructions"] = instructions
    assert json.loads(line) == expected
    assert rpc_command_from_json(line) == command


def test_get_session_stats_command_serializes_as_jsonl_and_parses() -> None:
    command = GetSessionStatsCommand(id="stats-1")

    assert json.loads(command.to_json_line()) == {
        "id": "stats-1",
        "type": "get_session_stats",
    }
    assert rpc_command_from_json(command.to_json_line()) == command


def test_get_state_command_serializes_as_jsonl_and_parses() -> None:
    command = GetStateCommand(id="state-1")

    assert json.loads(command.to_json_line()) == {
        "id": "state-1",
        "type": "get_state",
    }
    assert rpc_command_from_json(command.to_json_line()) == command


def test_get_messages_command_serializes_as_jsonl_and_parses() -> None:
    command = GetMessagesCommand(
        id="messages-1",
        session_id="session-1",
        limit=25,
        before_entry_id="entry-1",
    )

    assert json.loads(command.to_json_line()) == {
        "id": "messages-1",
        "type": "get_messages",
        "session_id": "session-1",
        "limit": 25,
        "before_entry_id": "entry-1",
    }
    assert rpc_command_from_json(command.to_json_line()) == command


def test_get_messages_command_rejects_invalid_bounds() -> None:
    with pytest.raises(ValidationError):
        GetMessagesCommand(limit=0)
    with pytest.raises(ValidationError):
        GetMessagesCommand(limit=501)
    with pytest.raises(ValidationError):
        GetMessagesCommand(session_id="")
    with pytest.raises(ValidationError):
        GetMessagesCommand(before_entry_id="")


def test_rpc_state_report_round_trips_only_at_schema_v16() -> None:
    event = RpcStateReported(
        command_id="state-1",
        state=RpcStateSnapshot(
            provider="fake",
            model="fake-model",
            effort=None,
            auto_compaction_enabled=True,
            steering_mode="one_at_a_time",
            follow_up_mode="one_at_a_time",
            pending_steering_count=0,
            pending_follow_up_count=0,
        ),
    )

    assert wisp_event_from_json(event.model_dump_json()) == event
    with pytest.raises(ValueError, match="require schema_version 16"):
        wisp_event_from_json(event.model_copy(update={"schema_version": 15}).model_dump_json())


def test_rpc_messages_report_round_trips_only_at_schema_v17() -> None:
    event = RpcMessagesReported(
        command_id="messages-1",
        session_id="session-1",
        session_path=Path("/tmp/session.jsonl"),
        active_leaf_id="entry-2",
        messages=(
            RpcMessageSnapshot(
                entry_id="entry-1",
                created_at=datetime(2026, 7, 23, tzinfo=UTC),
                role="assistant",
                content="running",
                content_original_bytes=7,
                tool_calls=(
                    RpcMessageToolCallSnapshot(
                        call_id="call-1",
                        name="bash",
                        arguments={"command": "pwd"},
                        arguments_original_bytes=17,
                    ),
                ),
            ),
        ),
    )

    assert wisp_event_from_json(event.model_dump_json()) == event
    with pytest.raises(ValueError, match="require schema_version 17"):
        wisp_event_from_json(event.model_copy(update={"schema_version": 16}).model_dump_json())


def test_rpc_state_snapshot_is_frozen_and_forbids_extra_fields() -> None:
    state = RpcStateSnapshot(
        provider="fake",
        model="fake-model",
        effort=None,
        auto_compaction_enabled=True,
        steering_mode="one_at_a_time",
        follow_up_mode="one_at_a_time",
        pending_steering_count=0,
        pending_follow_up_count=0,
    )

    with pytest.raises(ValidationError, match="frozen"):
        state.provider = "other"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RpcStateSnapshot.model_validate(
            {
                "provider": "fake",
                "model": "fake-model",
                "effort": None,
                "auto_compaction_enabled": True,
                "steering_mode": "one_at_a_time",
                "follow_up_mode": "one_at_a_time",
                "pending_steering_count": 0,
                "pending_follow_up_count": 0,
                "unexpected": True,
            }
        )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (SteerCommand(id="steer-1", content="redirect"), {"content": "redirect"}),
        (FollowUpCommand(id="follow-1", content="continue"), {"content": "continue"}),
        (GetQueueStateCommand(id="state-1"), {}),
        (
            SetQueueModeCommand(id="mode-1", kind="steering", mode="all"),
            {"kind": "steering", "mode": "all"},
        ),
        (PopQueueCommand(id="pop-1", kind="follow_up"), {"kind": "follow_up"}),
        (ClearQueueCommand(id="clear-1"), {}),
        (ClearQueueCommand(id="clear-2", kind="steering"), {"kind": "steering"}),
    ],
)
def test_queue_commands_serialize_as_jsonl_and_parse(
    command: RpcCommand,
    expected: dict[str, object],
) -> None:
    payload = json.loads(command.to_json_line())

    assert payload == {"id": command.id, "type": command.type, **expected}
    assert rpc_command_from_json(command.to_json_line()) == command


@pytest.mark.parametrize(
    "line",
    [
        '{"type":"set_queue_mode","kind":"unknown","mode":"all"}',
        '{"type":"set_queue_mode","kind":"steering","mode":"invalid"}',
        '{"type":"pop_queue","kind":"unknown"}',
        '{"type":"clear_queue","kind":"unknown"}',
    ],
)
def test_typed_queue_commands_reject_invalid_kinds_and_modes(line: str) -> None:
    with pytest.raises(ValueError):
        rpc_command_from_json(line)


def test_queue_items_removed_round_trips_through_json() -> None:
    event = QueueItemsRemoved(
        command_id="clear-1",
        operation="clear",
        steering=("first", "second"),
        follow_up=("later",),
    )

    assert wisp_event_from_json(event.model_dump_json()) == event
    with pytest.raises(ValueError, match="require schema_version 15"):
        wisp_event_from_json(event.model_copy(update={"schema_version": 14}).model_dump_json())


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"operation": "pop", "kind": None},
            "queue pop results require a queue kind",
        ),
        (
            {"operation": "pop", "kind": "steering", "steering": ("one", "two")},
            "at most one removed item",
        ),
        (
            {"operation": "clear", "kind": "steering", "follow_up": ("wrong",)},
            "cannot contain follow-up items",
        ),
        (
            {"operation": "clear", "kind": "follow_up", "steering": ("wrong",)},
            "cannot contain steering items",
        ),
    ],
)
def test_queue_items_removed_rejects_impossible_payloads(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        QueueItemsRemoved(command_id="queue-1", **kwargs)


def test_approval_scope_serializes_only_when_selected() -> None:
    scoped = ApprovalCommand(
        id="approval-1",
        call_id="call-1",
        approved=True,
        scope="tool_session",
    )

    assert json.loads(scoped.to_json_line()) == {
        "id": "approval-1",
        "type": "approval",
        "call_id": "call-1",
        "approved": True,
        "scope": "tool_session",
    }
    assert "scope" not in json.loads(
        ApprovalCommand(call_id="call-1", approved=True).to_json_line()
    )


def test_trust_command_serializes_as_jsonl_and_parses() -> None:
    command = TrustCommand(id="trust-1", request_id="req-1", trusted=True)

    line = command.to_json_line()

    assert json.loads(line) == {
        "id": "trust-1",
        "type": "trust",
        "request_id": "req-1",
        "trusted": True,
    }
    assert rpc_command_from_json(line) == command


def test_transient_trust_command_serializes_as_jsonl_and_parses() -> None:
    command = TrustCommand(
        id="trust-1",
        request_id="req-1",
        trusted=False,
        reason="Trust prompt closed",
        transient=True,
    )

    line = command.to_json_line()

    assert json.loads(line) == {
        "id": "trust-1",
        "type": "trust",
        "request_id": "req-1",
        "trusted": False,
        "reason": "Trust prompt closed",
        "transient": True,
    }
    assert rpc_command_from_json(line) == command


def test_trust_events_round_trip_through_json() -> None:
    requested = TrustRequested(request_id="req-1", project_path=Path("/repo"))
    resolved = TrustResolved(request_id="req-1", project_path=Path("/repo"), trusted=True)

    assert wisp_event_from_json(requested.model_dump_json()) == requested
    assert wisp_event_from_json(resolved.model_dump_json()) == resolved


def test_project_config_applied_round_trips_through_json() -> None:
    applied = ProjectConfigApplied(
        provider="openai", model="gpt-5.5", auth_path=Path("/home/u/.wisp/auth.json")
    )

    assert wisp_event_from_json(applied.model_dump_json()) == applied
    # model is optional (provider default).
    minimal = ProjectConfigApplied(provider="fake", auth_path=Path("/tmp/auth.json"))
    assert wisp_event_from_json(minimal.model_dump_json()) == minimal


def test_rpc_commands_allow_protocol_optional_id() -> None:
    command = PromptCommand(prompt="hello")

    line = command.to_json_line()
    parsed = rpc_command_from_json('{"type":"prompt","prompt":"hello"}')

    assert json.loads(line) == {"type": "prompt", "prompt": "hello"}
    assert command.id is None
    assert parsed == command


def test_wisp_event_from_json_returns_typed_event() -> None:
    event = wisp_event_from_json(
        '{"schema_version":6,"type":"rpc.command.finished","command_id":"cmd-1",'
        '"command_type":"prompt","ok":true}'
    )

    assert isinstance(event, RpcCommandFinished)
    assert event.command_id == "cmd-1"
    assert event.ok is True


def test_wisp_event_from_json_parses_provider_retry_progress() -> None:
    retry = ProviderRetrying(
        turn=1,
        provider="openai",
        attempt=2,
        max_attempts=3,
        delay_seconds=0.5,
        reason="rate_limit",
        status_code=429,
    )

    assert wisp_event_from_json(retry.model_dump_json()) == retry


@pytest.mark.parametrize("schema_version", [5, 16])
def test_wisp_event_from_json_accepts_legacy_schema_versions(schema_version: int) -> None:
    payload: dict[str, object] = {
        "type": "rpc.command.finished",
        "schema_version": schema_version,
        "command_id": "cmd-1",
        "command_type": "prompt",
        "ok": True,
    }

    assert wisp_event_from_json(json.dumps(payload)).schema_version == schema_version


@pytest.mark.parametrize("schema_version", [None, 1, 2, 3, 4])
def test_wisp_event_from_json_rejects_unsupported_schema_version(
    schema_version: int | None,
) -> None:
    payload: dict[str, object] = {
        "type": "rpc.command.finished",
        "command_id": "cmd-1",
        "command_type": "prompt",
        "ok": True,
    }
    if schema_version is not None:
        payload["schema_version"] = schema_version

    with pytest.raises(ValueError, match="Unsupported Wisp event schema_version"):
        wisp_event_from_json(json.dumps(payload))


def test_rpc_controller_sends_typed_commands_and_closes_transport() -> None:
    async def run() -> None:
        transport = RecordingTransport()
        controller = RpcController(
            transport,
            command_id_factory=lambda prefix: f"{prefix}-id",
        )

        prompt_id = await controller.prompt("hello")
        compact_id = await controller.compact("Keep paths")
        stats_id = await controller.get_session_stats()
        state_id = await controller.get_state()
        messages_id = await controller.get_messages(
            session_id="session-1",
            limit=25,
            before_entry_id="entry-1",
        )
        steer_id = await controller.steer("redirect")
        follow_up_id = await controller.follow_up("continue")
        queue_state_id = await controller.get_queue_state()
        queue_mode_id = await controller.set_queue_mode("steering", "all")
        queue_pop_id = await controller.pop_queue("steering")
        queue_clear_id = await controller.clear_queue("follow_up")
        cancel_id = await controller.cancel(prompt_id)
        approval_id = await controller.approve(
            "call-1",
            approved=True,
            scope="tool_session",
        )
        configure_id = await controller.configure(provider="openai-codex", model="gpt-5.5")
        shutdown_id = await controller.shutdown()
        await controller.close()

        assert [
            prompt_id,
            compact_id,
            stats_id,
            state_id,
            messages_id,
            steer_id,
            follow_up_id,
            queue_state_id,
            queue_mode_id,
            queue_pop_id,
            queue_clear_id,
            cancel_id,
            approval_id,
            configure_id,
            shutdown_id,
        ] == [
            "prompt-id",
            "compact-id",
            "stats-id",
            "state-id",
            "messages-id",
            "steer-id",
            "follow-up-id",
            "queue-state-id",
            "queue-mode-id",
            "queue-pop-id",
            "queue-clear-id",
            "cancel-id",
            "approval-id",
            "configure-id",
            "shutdown-id",
        ]
        assert transport.commands == [
            PromptCommand(id="prompt-id", prompt="hello"),
            CompactCommand(id="compact-id", instructions="Keep paths"),
            GetSessionStatsCommand(id="stats-id"),
            GetStateCommand(id="state-id"),
            GetMessagesCommand(
                id="messages-id",
                session_id="session-1",
                limit=25,
                before_entry_id="entry-1",
            ),
            SteerCommand(id="steer-id", content="redirect"),
            FollowUpCommand(id="follow-up-id", content="continue"),
            GetQueueStateCommand(id="queue-state-id"),
            SetQueueModeCommand(id="queue-mode-id", kind="steering", mode="all"),
            PopQueueCommand(id="queue-pop-id", kind="steering"),
            ClearQueueCommand(id="queue-clear-id", kind="follow_up"),
            CancelCommand(id="cancel-id", target_id="prompt-id"),
            ApprovalCommand(
                id="approval-id",
                call_id="call-1",
                approved=True,
                scope="tool_session",
            ),
            ConfigureCommand(id="configure-id", provider="openai-codex", model="gpt-5.5"),
            ShutdownCommand(id="shutdown-id"),
        ]
        assert transport.closed is True

    anyio.run(run)


def test_rpc_controller_configure_sends_effort() -> None:
    async def run() -> None:
        transport = RecordingTransport()
        controller = RpcController(
            transport,
            command_id_factory=lambda prefix: f"{prefix}-id",
        )

        await controller.configure(effort="high")

        assert transport.commands == [
            ConfigureCommand(id="configure-id", effort="high"),
        ]

    anyio.run(run)


def test_configure_command_serializes_effort_and_omits_when_unset() -> None:
    with_effort = ConfigureCommand(id="configure-1", effort="medium")

    line = with_effort.to_json_line()

    assert json.loads(line) == {
        "id": "configure-1",
        "type": "configure",
        "effort": "medium",
        "clear_effort": False,
    }
    assert rpc_command_from_json(line) == with_effort

    without_effort = ConfigureCommand(id="configure-2", model="gpt-5.5")

    assert "effort" not in json.loads(without_effort.to_json_line())


def test_rpc_controller_configure_clear_effort() -> None:
    # Regression test: effort=None is indistinguishable on the wire from
    # never having set effort at all (exclude_none drops it), so a client
    # that previously set an effort tier has no way to reset it back to the
    # provider's own default without a distinct, always-serialized signal.
    async def run() -> None:
        transport = RecordingTransport()
        controller = RpcController(
            transport,
            command_id_factory=lambda prefix: f"{prefix}-id",
        )

        await controller.configure(clear_effort=True)

        assert transport.commands == [
            ConfigureCommand(id="configure-id", clear_effort=True),
        ]

    anyio.run(run)


def test_configure_command_always_serializes_clear_effort() -> None:
    # clear_effort's default (False) must never be silently dropped the way
    # effort=None is -- it is the only signal the server has to distinguish
    # "leave effort untouched" from "explicitly reset effort."
    command = ConfigureCommand(id="configure-1", model="gpt-5.5")

    assert json.loads(command.to_json_line())["clear_effort"] is False


def test_rpc_controller_exposes_transport_events() -> None:
    async def run() -> None:
        expected_events = [
            RpcCommandStarted(command_id="cmd-1", command_type="prompt"),
            RpcCommandFinished(command_id="cmd-1", command_type="prompt", ok=True),
        ]
        controller = RpcController(RecordingTransport(events=expected_events))

        events = [event async for event in controller.events()]

        assert events == expected_events

    anyio.run(run)


def test_jsonl_subprocess_rpc_transport_round_trips_events(tmp_path: Path) -> None:
    async def run() -> None:
        script = """
import json
import sys
command = json.loads(sys.stdin.readline())
started = {
    "schema_version": 6,
    "type": "rpc.command.started",
    "command_id": command["id"],
    "command_type": command["type"],
}
finished = {
    "schema_version": 6,
    "type": "rpc.command.finished",
    "command_id": command["id"],
    "command_type": command["type"],
    "ok": True,
}
print(json.dumps(started), flush=True)
print(json.dumps(finished), flush=True)
"""
        transport = await JsonlSubprocessRpcTransport.start(
            [sys.executable, "-c", script],
            cwd=tmp_path,
        )
        controller = RpcController(transport, command_id_factory=lambda _prefix: "shutdown-1")

        await controller.shutdown()
        events = [event async for event in controller.events()]
        await controller.close()

        assert [event.type for event in events] == [
            "rpc.command.started",
            "rpc.command.finished",
        ]
        assert isinstance(events[0], RpcCommandStarted)
        assert isinstance(events[1], RpcCommandFinished)
        assert events[0].command_id == "shutdown-1"
        assert events[1].ok is True

    anyio.run(run)


def test_jsonl_subprocess_rpc_transport_does_not_block_on_stderr(tmp_path: Path) -> None:
    async def run() -> None:
        script = """
import json
import sys
sys.stderr.write("x" * 200000)
sys.stderr.flush()
command = json.loads(sys.stdin.readline())
print(json.dumps({
    "schema_version": 6,
    "type": "rpc.command.finished",
    "command_id": command["id"],
    "command_type": command["type"],
    "ok": True,
}), flush=True)
"""
        transport = await JsonlSubprocessRpcTransport.start(
            [sys.executable, "-c", script],
            cwd=tmp_path,
        )
        controller = RpcController(transport, command_id_factory=lambda _prefix: "shutdown-1")

        await controller.shutdown()
        with anyio.fail_after(5):
            events = [event async for event in controller.events()]
        await controller.close()

        assert [event.type for event in events] == ["rpc.command.finished"]
        assert isinstance(events[0], RpcCommandFinished)
        assert events[0].command_id == "shutdown-1"
        assert events[0].ok is True

    anyio.run(run)
