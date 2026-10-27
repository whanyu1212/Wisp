from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.support.paths import REPO_ROOT
from wisp.events import (
    ProjectConfigApplied,
    ProviderRetrying,
    QueueItemsRemoved,
    RpcCommandArgument,
    RpcCommandDescriptor,
    RpcCommandsReported,
    RpcConnectionCatalogReported,
    RpcConnectionCatalogSnapshot,
    RpcConnectionMethodSnapshot,
    RpcConnectionProviderSnapshot,
    RpcMessageSnapshot,
    RpcMessagesReported,
    RpcMessageToolCallSnapshot,
    RpcMessageToolResultSnapshot,
    RpcModelCatalogEntry,
    RpcModelCatalogReported,
    RpcModelCatalogSnapshot,
    RpcModelProviderSnapshot,
    RpcModelSelectionSnapshot,
    RpcSessionCloned,
    RpcSessionNameChanged,
    RpcSessionSelected,
    RpcSessionsReported,
    RpcSessionSummary,
    RpcSessionTreeNavigated,
    RpcSessionTreeNode,
    RpcSessionTreeReported,
    RpcStateReported,
    RpcStateSnapshot,
    ToolResultReady,
    wisp_event_from_json,
)


def test_changelog_documents_protocol_as_the_single_event_contract() -> None:
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "Events no longer carry a per-event `schema_version`." in changelog


def test_connection_catalog_event_round_trips_and_enforces_bounds() -> None:
    method = RpcConnectionMethodSnapshot(
        provider="anthropic",
        label="Anthropic API key",
        kind="api_key",
        source="missing",
        environment_variable="ANTHROPIC_API_KEY",
    )
    provider = RpcConnectionProviderSnapshot(
        id="anthropic",
        label="Anthropic",
        methods=(method,),
    )
    event = RpcConnectionCatalogReported(
        command_id="connections-1",
        catalog=RpcConnectionCatalogSnapshot(providers=(provider,)),
    )

    assert wisp_event_from_json(event.model_dump_json()) == event
    with pytest.raises(ValidationError):
        RpcConnectionCatalogSnapshot(providers=(provider,) * 33)


def test_model_catalog_event_round_trips_and_enforces_bounds() -> None:
    model = RpcModelCatalogEntry(id="model-1", effort_levels=("low", "high"))
    provider = RpcModelProviderSnapshot(
        name="provider",
        display_name="Provider",
        default_model="model-1",
        available=True,
        models=(model,),
    )
    selection = RpcModelSelectionSnapshot(
        provider="provider",
        model=None,
        effective_model="model-1",
        catalog_model="model-1",
        effort="high",
    )
    event = RpcModelCatalogReported(
        command_id="models-1",
        catalog=RpcModelCatalogSnapshot(selection=selection, providers=(provider,)),
    )

    assert wisp_event_from_json(event.model_dump_json()) == event
    with pytest.raises(ValidationError):
        RpcModelProviderSnapshot(
            name="provider",
            display_name="Provider",
            default_model="model-1",
            available=True,
            models=(model,) * 513,
        )
    with pytest.raises(ValidationError):
        RpcModelCatalogSnapshot(selection=selection, providers=(provider,) * 129)
    with pytest.raises(ValidationError, match="at most 4096 models"):
        full_provider = provider.model_copy(update={"models": (model,) * 512})
        RpcModelCatalogSnapshot(selection=selection, providers=(full_provider,) * 9)
    with pytest.raises(ValidationError):
        RpcModelCatalogEntry(id="model-1", effort_levels=("effort",) * 17)
    with pytest.raises(ValidationError):
        RpcModelSelectionSnapshot(provider="p" * 129)
    with pytest.raises(ValidationError):
        RpcModelCatalogEntry(id="m" * 257)
    with pytest.raises(ValidationError):
        RpcModelProviderSnapshot(
            name="provider",
            display_name="d" * 257,
            default_model="model-1",
            available=True,
        )
    with pytest.raises(ValidationError):
        RpcModelCatalogEntry(id="model-1", effort_levels=("e" * 65,))


def test_rpc_state_agent_mode_defaults_to_build_when_absent() -> None:
    event = RpcStateReported(
        command_id="state-1",
        state=RpcStateSnapshot(
            provider="fake",
            mode="plan",
            auto_compaction_enabled=True,
            steering_mode="one_at_a_time",
            follow_up_mode="one_at_a_time",
            pending_steering_count=0,
            pending_follow_up_count=0,
        ),
    )
    payload = json.loads(event.model_dump_json())
    del payload["state"]["mode"]

    restored = wisp_event_from_json(json.dumps(payload))
    assert isinstance(restored, RpcStateReported)
    assert restored.state.mode == "build"


def test_rpc_commands_report_round_trips() -> None:
    event = RpcCommandsReported(
        command_id="commands-1",
        commands=(
            RpcCommandDescriptor(
                name="compact",
                title="Compact",
                description="Compact the session context",
                category="session",
                aliases=("cx",),
                slash_command="/compact",
                slash_aliases=("/cx",),
                arguments=(
                    RpcCommandArgument(
                        name="instructions",
                        description="Optional compaction guidance",
                    ),
                ),
                accepts_arguments=True,
                order=20,
            ),
        ),
    )

    assert wisp_event_from_json(event.model_dump_json()) == event


def test_rpc_messages_report_round_trips() -> None:
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


def test_rpc_messages_tool_result_metadata_is_tool_role_only() -> None:
    event = RpcMessagesReported(
        command_id="messages-1",
        messages=(
            RpcMessageSnapshot(
                entry_id="entry-1",
                created_at=datetime(2026, 7, 28, tzinfo=UTC),
                role="tool",
                content="boom",
                content_original_bytes=4,
                tool_call_id="call-1",
                tool_name="bash",
                is_error=False,
                tool_result=RpcMessageToolResultSnapshot(
                    exit_code=1,
                    output_has_exit_status=True,
                    summary="command failed",
                    truncated=True,
                ),
            ),
        ),
    )

    assert wisp_event_from_json(event.model_dump_json()) == event
    with pytest.raises(ValueError, match="valid only on tool messages"):
        RpcMessageSnapshot(
            entry_id="entry-2",
            created_at=datetime(2026, 7, 28, tzinfo=UTC),
            role="assistant",
            content="not a tool",
            content_original_bytes=10,
            tool_result=RpcMessageToolResultSnapshot(summary="invalid"),
        )


def test_rpc_sessions_report_round_trips() -> None:
    event = RpcSessionsReported(
        command_id="sessions-1",
        sessions=(
            RpcSessionSummary(
                session_id="session-1",
                session_path=Path("/tmp/session-1.jsonl"),
                updated_at=datetime(2026, 7, 24, tzinfo=UTC),
                entry_count=3,
                active_leaf_id="entry-3",
            ),
        ),
        selected_session_id="session-1",
        selected_session_path=Path("/tmp/session-1.jsonl"),
    )

    assert wisp_event_from_json(event.model_dump_json()) == event


def test_rpc_session_derivation_events_reject_invalid_targets() -> None:
    fields = {
        "command_id": "clone-1",
        "source_session_id": "same",
        "source_session_path": Path("/tmp/source.jsonl"),
        "source_active_leaf_id": "entry-1",
        "session_id": "same",
        "session_path": Path("/tmp/clone.jsonl"),
        "active_leaf_id": "entry-1",
        "entry_count": 1,
    }
    with pytest.raises(ValidationError, match="must create a new session id"):
        RpcSessionCloned(**fields)
    with pytest.raises(ValidationError, match="require an active leaf"):
        RpcSessionCloned(**{**fields, "session_id": "clone", "active_leaf_id": None})


def test_rpc_session_tree_events_round_trip() -> None:
    node = RpcSessionTreeNode(
        entry_id="entry-1",
        parent_id=None,
        operation_id="prompt-1",
        created_at=datetime(2026, 7, 24, tzinfo=UTC),
        kind="message",
        role="user",
        preview="edit me",
    )
    report = RpcSessionTreeReported(
        command_id="tree-1",
        session_id="session-1",
        session_path=Path("/tmp/session-1.jsonl"),
        active_leaf_id="entry-1",
        total_node_count=2,
        nodes=(node,),
        truncated=True,
        next_after_entry_id="entry-1",
    )
    navigated = RpcSessionTreeNavigated(
        command_id="navigate-1",
        session_id="session-1",
        session_path=Path("/tmp/session-1.jsonl"),
        selected_entry_id="entry-1",
        previous_active_leaf_id="entry-2",
        active_leaf_id=None,
        editor_text="edit me",
        changed=True,
        entry_count=3,
    )

    assert wisp_event_from_json(report.model_dump_json()) == report
    assert wisp_event_from_json(navigated.model_dump_json()) == navigated


def test_rpc_message_forward_cursor_round_trips() -> None:
    report = RpcMessagesReported(
        command_id="messages-1",
        truncated=True,
        next_after_entry_id="entry-1",
    )

    assert wisp_event_from_json(report.model_dump_json()) == report

    without_cursor = RpcMessagesReported(command_id="messages-plain")
    payload = json.loads(without_cursor.model_dump_json())
    assert payload["next_after_entry_id"] is None
    assert wisp_event_from_json(json.dumps(payload)) == without_cursor


def test_tool_failure_metadata_round_trips() -> None:
    event = ToolResultReady(
        call_id="call-1",
        name="grep",
        output="Invalid grep pattern\nRecovery: Retry with literal=true.",
        is_error=True,
        failure_code="invalid_pattern",
        retryable=True,
        recovery_hint="Retry with literal=true.",
    )

    assert wisp_event_from_json(event.model_dump_json()) == event


def test_tool_failure_metadata_requires_an_error_and_failure_code() -> None:
    with pytest.raises(ValidationError, match="requires is_error=true"):
        ToolResultReady(
            call_id="call-1",
            name="grep",
            output="bad pattern",
            is_error=False,
            failure_code="invalid_pattern",
        )
    with pytest.raises(ValidationError, match="requires a tool failure code"):
        ToolResultReady(
            call_id="call-1",
            name="grep",
            output="bad pattern",
            is_error=True,
            retryable=True,
        )


def test_rpc_session_name_changed_round_trips() -> None:
    event = RpcSessionNameChanged(
        command_id="name-1",
        session_id="session-1",
        session_path=Path("/tmp/session-1.jsonl"),
        previous_name="Old",
        name=None,
        entry_count=4,
    )

    assert wisp_event_from_json(event.model_dump_json()) == event


@pytest.mark.parametrize(
    "payload",
    [
        {
            "type": "rpc.state",
            "command_id": "state-1",
            "state": {
                "provider": "fake",
                "model": "fake-model",
                "effort": None,
                "auto_compaction_enabled": True,
                "steering_mode": "one_at_a_time",
                "follow_up_mode": "one_at_a_time",
                "pending_steering_count": 0,
                "pending_follow_up_count": 0,
                "session_name": "Named",
                "active_command_id": None,
                "active_command_type": None,
                "cancel_requested": False,
            },
        },
        {
            "type": "rpc.sessions",
            "command_id": "sessions-1",
            "sessions": [],
            "selected_session_id": "session-1",
            "selected_session_path": "/tmp/session-1.jsonl",
            "selected_session_name": "Named",
        },
        {
            "type": "rpc.sessions",
            "command_id": "sessions-1",
            "sessions": [
                {
                    "session_id": "session-1",
                    "session_path": "/tmp/session-1.jsonl",
                    "updated_at": "2026-07-24T00:00:00Z",
                    "entry_count": 1,
                    "active_leaf_id": "entry-1",
                    "name": "Named",
                }
            ],
        },
        {
            "type": "rpc.session.selected",
            "command_id": "select-1",
            "session_id": "session-1",
            "session_path": "/tmp/session-1.jsonl",
            "active_leaf_id": "entry-1",
            "entry_count": 1,
            "session_name": "Named",
        },
        {
            "type": "rpc.session.cloned",
            "command_id": "clone-1",
            "source_session_id": "source",
            "source_session_path": "/tmp/source.jsonl",
            "source_active_leaf_id": "entry-1",
            "source_session_name": "Source",
            "session_id": "clone",
            "session_path": "/tmp/clone.jsonl",
            "active_leaf_id": "entry-1",
            "session_name": "Clone",
            "entry_count": 1,
        },
        {
            "type": "rpc.session.forked",
            "command_id": "fork-1",
            "source_session_id": "source",
            "source_session_path": "/tmp/source.jsonl",
            "source_active_leaf_id": "entry-2",
            "source_session_name": "Source",
            "session_id": "fork",
            "session_path": "/tmp/fork.jsonl",
            "active_leaf_id": "entry-1",
            "session_name": "Fork",
            "entry_count": 1,
            "selected_entry_id": "entry-2",
            "selected_prompt": "edit me",
        },
    ],
)
def test_rpc_session_name_fields_round_trip(payload: dict[str, object]) -> None:
    event = wisp_event_from_json(json.dumps(payload))
    dumped = json.loads(event.model_dump_json())
    _assert_subset(payload, dumped)


def _assert_subset(expected: object, actual: object) -> None:
    """Assert every key in ``expected`` appears in ``actual``; defaults may add more."""

    if isinstance(expected, dict):
        assert isinstance(actual, dict)
        for key, value in expected.items():
            assert key in actual, key
            _assert_subset(value, actual[key])
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected)
        for left, right in zip(expected, actual, strict=True):
            _assert_subset(left, right)
    else:
        assert expected == actual


def test_rpc_session_tree_events_reject_inconsistent_payloads() -> None:
    with pytest.raises(ValidationError, match="session_id and session_path together"):
        RpcSessionTreeReported(
            command_id="tree-1",
            session_id="session-1",
            total_node_count=0,
        )
    with pytest.raises(ValidationError, match="next_after_entry_id exactly when truncated"):
        RpcSessionTreeReported(
            command_id="tree-1",
            total_node_count=0,
            truncated=True,
        )
    node = RpcSessionTreeNode(
        entry_id="entry-1",
        created_at=datetime(2026, 7, 24, tzinfo=UTC),
        kind="message",
        role="user",
        preview="one",
    )
    with pytest.raises(ValidationError, match="final returned node"):
        RpcSessionTreeReported(
            command_id="tree-1",
            session_id="session-1",
            session_path=Path("/tmp/session-1.jsonl"),
            total_node_count=2,
            nodes=(node,),
            truncated=True,
            next_after_entry_id="entry-2",
        )
    with pytest.raises(ValidationError, match="without a session must be empty"):
        RpcSessionTreeReported(
            command_id="tree-1",
            active_leaf_id="entry-1",
            total_node_count=1,
            nodes=(node,),
        )
    with pytest.raises(ValidationError, match="include role only for messages"):
        RpcSessionTreeNode(
            entry_id="entry-1",
            created_at=datetime(2026, 7, 24, tzinfo=UTC),
            kind="event",
            role="user",
            preview="error",
        )
    with pytest.raises(ValidationError, match="must match the active-leaf transition"):
        RpcSessionTreeNavigated(
            command_id="navigate-1",
            session_id="session-1",
            session_path=Path("/tmp/session-1.jsonl"),
            selected_entry_id="entry-1",
            previous_active_leaf_id="entry-2",
            active_leaf_id="entry-2",
            editor_text="edit",
            changed=True,
            entry_count=3,
        )


def test_rpc_session_events_reject_incomplete_or_empty_identity() -> None:
    with pytest.raises(ValidationError, match="String should have at least 1 character"):
        RpcSessionSummary(
            session_id="",
            session_path=Path("/tmp/session-1.jsonl"),
            updated_at=datetime(2026, 7, 24, tzinfo=UTC),
            entry_count=0,
        )
    with pytest.raises(ValidationError, match="String should have at least 1 character"):
        RpcSessionSummary(
            session_id="session-1",
            session_path=Path("/tmp/session-1.jsonl"),
            updated_at=datetime(2026, 7, 24, tzinfo=UTC),
            entry_count=0,
            active_leaf_id="",
        )
    with pytest.raises(ValidationError, match="selected_session_id and selected_session_path"):
        RpcSessionsReported(command_id="sessions-1", selected_session_id="session-1")
    with pytest.raises(ValidationError, match="selected_session_id and selected_session_path"):
        RpcSessionsReported(
            command_id="sessions-1",
            selected_session_path=Path("/tmp/session-1.jsonl"),
        )
    with pytest.raises(ValidationError, match="String should have at least 1 character"):
        RpcSessionSelected(
            command_id="select-1",
            session_id="",
            session_path=Path("/tmp/session-1.jsonl"),
            entry_count=0,
        )


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


def test_queue_items_removed_round_trips_through_json() -> None:
    event = QueueItemsRemoved(
        command_id="clear-1",
        operation="clear",
        steering=("first", "second"),
        follow_up=("later",),
    )

    assert wisp_event_from_json(event.model_dump_json()) == event


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


def test_project_config_applied_round_trips_through_json() -> None:
    applied = ProjectConfigApplied(
        provider="openai", model="gpt-5.5", auth_path=Path("/home/u/.wisp/auth.json")
    )

    assert wisp_event_from_json(applied.model_dump_json()) == applied
    # model is optional (provider default).
    minimal = ProjectConfigApplied(provider="fake", auth_path=Path("/tmp/auth.json"))
    assert wisp_event_from_json(minimal.model_dump_json()) == minimal

    # Older writers omitted the compaction policy; absence reads as "unknown".
    payload = json.loads(applied.model_dump_json())
    del payload["auto_compaction_enabled"]
    restored = wisp_event_from_json(json.dumps(payload))
    assert isinstance(restored, ProjectConfigApplied)
    assert restored.auto_compaction_enabled is None


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


def test_wisp_event_from_json_rejects_per_event_schema_version() -> None:
    """Live events carry no version; pre-v9 payloads are only accepted via session replay."""

    payload: dict[str, object] = {
        "type": "rpc.command.finished",
        "schema_version": 5,
        "command_id": "cmd-1",
        "command_type": "prompt",
        "ok": True,
    }

    with pytest.raises(ValidationError, match="schema_version"):
        wisp_event_from_json(json.dumps(payload))
