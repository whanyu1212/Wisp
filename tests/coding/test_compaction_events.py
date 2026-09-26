from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from wisp.agent.context_budget import build_context_budget, estimate_context
from wisp.agent.messages import Message
from wisp.events import (
    CompactionCompleted,
    CompactionStarted,
    KnownWispEventAdapter,
    SessionSaved,
    TokenUsage,
    wisp_event_from_dict,
    wisp_event_from_json,
)


def test_compaction_events_round_trip_without_summary() -> None:
    started = CompactionStarted(session_id="session", source_entry_count=4)
    completed = CompactionCompleted(
        session_id="session",
        outcome="completed",
        compaction_id="compact",
        replaced_entry_count=2,
        retained_entry_count=2,
        provider="test",
        model="model",
        usage=TokenUsage(input_tokens=3, output_tokens=2, total_tokens=5),
    )

    assert "summary" not in completed.model_dump(mode="json")
    assert wisp_event_from_json(started.model_dump_json()) == started
    assert wisp_event_from_json(completed.model_dump_json()) == completed
    with pytest.raises(ValidationError):
        CompactionCompleted(
            session_id="session",
            outcome="completed",
            replaced_entry_count=-1,
            retained_entry_count=0,
        )


def test_event_parser_rejects_legacy_per_event_schema_version() -> None:
    """Live parsing fails closed on the pre-v9 ``schema_version`` key."""

    payload = {
        "schema_version": 11,
        "type": "session.saved",
        "session_id": "session",
        "path": "/tmp/session.jsonl",
    }

    with pytest.raises(ValidationError, match="schema_version"):
        wisp_event_from_dict(payload)
    with pytest.raises(ValidationError, match="schema_version"):
        SessionSaved(**payload)
    with pytest.raises(ValidationError, match="schema_version"):
        KnownWispEventAdapter.validate_python(payload)


def test_threshold_compaction_events_round_trip() -> None:
    estimate = estimate_context((Message(role="user", content="hello"),))
    budget = build_context_budget(estimate, context_window=100, reserve_tokens=20)
    event = CompactionStarted(
        session_id="session",
        reason="threshold",
        source_entry_count=4,
        trigger_budget=budget,
    )

    assert wisp_event_from_json(event.model_dump_json()) == event


def test_overflow_compaction_events_validate_retry_metadata() -> None:
    estimate = estimate_context((Message(role="user", content="hello"),))
    budget = build_context_budget(estimate, context_window=100, reserve_tokens=20)
    started = CompactionStarted(
        session_id="session",
        reason="overflow",
        source_entry_count=4,
        trigger_budget=budget,
    )
    completed = CompactionCompleted(
        session_id="session",
        reason="overflow",
        outcome="completed",
        replaced_entry_count=2,
        retained_entry_count=2,
        will_retry=True,
    )

    assert wisp_event_from_json(started.model_dump_json()) == started
    assert wisp_event_from_json(completed.model_dump_json()) == completed
    with pytest.raises(ValidationError, match="without retry must explain"):
        CompactionCompleted(
            session_id="session",
            reason="overflow",
            outcome="completed",
            replaced_entry_count=2,
            retained_entry_count=2,
        )
    assert (
        CompactionCompleted(
            session_id="session",
            reason="overflow",
            outcome="completed",
            replaced_entry_count=2,
            retained_entry_count=2,
            error="retry setup failed",
        ).will_retry
        is False
    )
    with pytest.raises(ValidationError, match="without retry must explain"):
        CompactionCompleted(
            session_id="session",
            reason="overflow",
            outcome="completed",
            replaced_entry_count=2,
            retained_entry_count=2,
            error="",
        )
    with pytest.raises(ValidationError, match="only overflow compaction may retry"):
        CompactionCompleted(
            session_id="session",
            outcome="completed",
            replaced_entry_count=2,
            retained_entry_count=2,
            will_retry=True,
        )


def test_compaction_started_without_trigger_budget_round_trips() -> None:
    payload = json.dumps(
        {
            "type": "compaction.started",
            "timestamp": "2026-07-19T00:00:00Z",
            "session_id": "session",
            "source_entry_count": 4,
        }
    )

    event = wisp_event_from_json(payload)

    assert event.model_dump(mode="json")["trigger_budget"] is None
    assert wisp_event_from_json(event.model_dump_json()) == event


def test_compaction_started_validates_reason_metadata() -> None:
    estimate = estimate_context((Message(role="user", content="hello"),))
    budget = build_context_budget(estimate, context_window=100, reserve_tokens=20)

    with pytest.raises(ValidationError, match="requires a trigger budget"):
        CompactionStarted(
            session_id="session",
            reason="threshold",
            source_entry_count=4,
        )
    with pytest.raises(ValidationError, match="manual compaction must not include"):
        CompactionStarted(
            session_id="session",
            source_entry_count=4,
            trigger_budget=budget,
        )
