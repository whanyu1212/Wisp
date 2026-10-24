"""Unit tests for assert_cancellation_settled on hand-built event sequences."""

from __future__ import annotations

import pytest

from tests.agent.invariant_checks.support import (
    completed_turn,
    tool_ended,
    tool_ready,
)
from tests.support.agent_runtime import assert_cancellation_settled
from wisp.events import (
    ErrorEvent,
    MessageDelta,
    QueueMessageInjected,
    TurnCompleted,
    TurnStarted,
)


def test_accepts_clean_cancellation() -> None:
    events = (
        TurnStarted(turn=1),
        ErrorEvent(message="Agent run cancelled"),
        TurnCompleted(turn=1, outcome="cancelled", finish_reason="cancelled"),
    )
    assert_cancellation_settled(events)


def test_accepts_pre_turn_cancellation() -> None:
    events = (ErrorEvent(message="Agent run cancelled"),)
    assert_cancellation_settled(events)


@pytest.mark.parametrize(
    "trailing",
    [
        ErrorEvent(message="Late error"),
        QueueMessageInjected(kind="follow_up", content="Late follow-up"),
    ],
)
def test_rejects_trailing_boundary_events(trailing: object) -> None:
    events = (
        TurnStarted(turn=1),
        ErrorEvent(message="Agent run cancelled"),
        TurnCompleted(turn=1, outcome="cancelled", finish_reason="cancelled"),
        trailing,
    )
    with pytest.raises(AssertionError, match="after cancelled TurnCompleted"):
        assert_cancellation_settled(events)


@pytest.mark.parametrize("position", ["before", "after", "next_turn"])
def test_rejects_activity_outside_turn(position: str) -> None:
    events: list[object] = [
        TurnStarted(turn=1),
        ErrorEvent(message="Agent run cancelled"),
        TurnCompleted(turn=1, outcome="cancelled", finish_reason="cancelled"),
    ]
    if position == "before":
        events.insert(0, MessageDelta(turn=1, delta="Too early"))
        match = "outside an active turn"
    elif position == "after":
        events.append(MessageDelta(turn=1, delta="Too late"))
        match = "after final TurnCompleted"
    else:
        events.extend(
            [
                TurnStarted(turn=2),
                ErrorEvent(message="Agent run cancelled again"),
                TurnCompleted(turn=2, outcome="cancelled", finish_reason="cancelled"),
            ]
        )
        match = "after turn 1 was cancelled"
    with pytest.raises(AssertionError, match=match):
        assert_cancellation_settled(events)


def test_rejects_activity_before_first_turn() -> None:
    events = (
        tool_ended("call-1"),
        tool_ready("call-1"),
        ErrorEvent(message="Agent run cancelled"),
    )
    with pytest.raises(AssertionError, match="Pre-turn cancellation produced turn-scoped events"):
        assert_cancellation_settled(events)


def test_accepts_error_after_completed_turn() -> None:
    events = (
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="stop"),
        ErrorEvent(message="Agent run cancelled"),
    )
    assert_cancellation_settled(events)


@pytest.mark.parametrize("boundary", ["pre_turn", "completed", "failed"])
@pytest.mark.parametrize("tail_kind", ["queue_then_error", "error_then_queue", "two_errors"])
def test_requires_one_final_boundary_error(boundary: str, tail_kind: str) -> None:
    events: list[object] = []
    if boundary == "completed":
        events.extend(completed_turn())
    elif boundary == "failed":
        events.extend(
            [
                TurnStarted(turn=1),
                ErrorEvent(message="Earlier provider failure"),
                TurnCompleted(turn=1, outcome="failed", finish_reason="error"),
            ]
        )
    error = ErrorEvent(message="Agent run cancelled")
    queued = QueueMessageInjected(kind="follow_up", content="Next message")
    if tail_kind == "queue_then_error":
        events.extend([queued, error])
    else:
        events.extend([error, queued if tail_kind == "error_then_queue" else error])
    if tail_kind == "queue_then_error" and boundary != "pre_turn":
        assert_cancellation_settled(events)
    else:
        with pytest.raises(AssertionError, match="must end with exactly one ErrorEvent"):
            assert_cancellation_settled(events)


def test_accepts_aborted_provider_wording() -> None:
    events = (
        TurnStarted(turn=1),
        ErrorEvent(message="request aborted"),
        TurnCompleted(turn=1, outcome="cancelled", finish_reason="cancelled"),
    )
    assert_cancellation_settled(events)


def test_rejects_ended_without_ready() -> None:
    events = (
        TurnStarted(turn=1),
        tool_ended("call-1"),
        ErrorEvent(message="Agent run cancelled"),
        TurnCompleted(turn=1, outcome="cancelled", finish_reason="cancelled"),
    )
    with pytest.raises(AssertionError, match="without ToolResultReady"):
        assert_cancellation_settled(events)


def test_rejects_missing_error_event() -> None:
    events = (
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="cancelled", finish_reason="cancelled"),
    )
    with pytest.raises(AssertionError, match="ErrorEvent before completion"):
        assert_cancellation_settled(events)


@pytest.mark.parametrize("earlier_turn", [True, False])
def test_rejects_nonadjacent_error(earlier_turn: bool) -> None:
    events: list[object] = [TurnStarted(turn=1), ErrorEvent(message="Earlier failure")]
    if earlier_turn:
        events.extend(
            [
                TurnCompleted(turn=1, outcome="failed", finish_reason="error"),
                TurnStarted(turn=2),
            ]
        )
    else:
        events.append(MessageDelta(turn=1, delta="Still running"))
    events.append(
        TurnCompleted(turn=2 if earlier_turn else 1, outcome="cancelled", finish_reason="cancelled")
    )
    with pytest.raises(AssertionError, match="immediately adjacent"):
        assert_cancellation_settled(events)


def test_rejects_completed_outcome() -> None:
    events = (
        TurnStarted(turn=1),
        ErrorEvent(message="Agent run cancelled"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="outcome 'cancelled'"):
        assert_cancellation_settled(events)
