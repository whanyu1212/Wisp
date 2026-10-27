"""Unit tests for assert_turn_terminals and assert_turn_invariants."""

from __future__ import annotations

import pytest

from tests.agent.invariant_checks.support import (
    completed_turn,
    tool_ended,
    tool_ready,
)
from tests.support.agent_runtime import (
    assert_turn_invariants,
    assert_turn_terminals,
)
from wisp.events import (
    ErrorEvent,
    MessageCompleted,
    MessageDelta,
    MessageStarted,
    ToolCallRequested,
    TurnCompleted,
    TurnStarted,
)


def test_accepts_matched_starts_and_completes() -> None:
    assert_turn_terminals(completed_turn())
    assert_turn_terminals((*completed_turn(1), *completed_turn(2)))


def test_accepts_unstarted_failure() -> None:
    assert_turn_terminals((ErrorEvent(message="boom"),))


def test_rejects_missing_terminal() -> None:
    with pytest.raises(AssertionError, match="started without a terminal"):
        assert_turn_terminals((TurnStarted(turn=1),))


def test_rejects_unstarted_complete() -> None:
    with pytest.raises(AssertionError, match="without a matching TurnStarted"):
        assert_turn_terminals((TurnCompleted(turn=1, outcome="completed", finish_reason="stop"),))


def test_rejects_double_complete() -> None:
    with pytest.raises(AssertionError, match="completed more than once"):
        assert_turn_terminals(
            (
                *completed_turn(),
                TurnCompleted(turn=1, outcome="failed", finish_reason="error"),
            )
        )


def test_accepts_clean_turn_sequence() -> None:
    events = (
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    assert_turn_invariants(events)


def test_rejects_unexpected_initial_turn() -> None:
    events = (
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="expected turn 1"):
        assert_turn_invariants(events, initial_turn=1)


def test_rejects_turn_gap() -> None:
    events = (
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        TurnStarted(turn=3),
        TurnCompleted(turn=3, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="expected turn 2"):
        assert_turn_invariants(events)


def test_rejects_invalid_outcome() -> None:
    events = (
        TurnStarted(turn=1),
        TurnCompleted.model_construct(turn=1, outcome="unknown", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="Invalid TurnCompleted outcome"):
        assert_turn_invariants(events)


@pytest.mark.parametrize(
    ("completed", "match"),
    [
        (
            TurnCompleted(turn=1, outcome="cancelled", finish_reason="stop"),
            "finish_reason 'cancelled'",
        ),
        (
            TurnCompleted(turn=1, outcome="completed", finish_reason="cancelled"),
            "must not have finish_reason 'cancelled'",
        ),
        (
            TurnCompleted(turn=1, outcome="completed", finish_reason="error"),
            "must not have finish_reason 'error'",
        ),
        (
            TurnCompleted(turn=1, outcome="failed", finish_reason="stop"),
            "Failed turn must have finish_reason 'error'",
        ),
    ],
    ids=["cancelled-stop", "completed-cancelled", "completed-error", "failed-stop"],
)
def test_rejects_mismatched_finish_reason(completed: TurnCompleted, match: str) -> None:
    with pytest.raises(AssertionError, match=match):
        assert_turn_invariants((TurnStarted(turn=1), completed))


def test_rejects_activity_after_cancellation() -> None:
    events = (
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="cancelled", finish_reason="cancelled"),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="appeared after turn 1 was cancelled"):
        assert_turn_invariants(events)


@pytest.mark.parametrize(
    "late",
    [
        tool_ended("call-1"),
        ToolCallRequested(call_id="call-1", name="lookup", arguments={}),
        MessageCompleted(turn=1, content="late", finish_reason="stop"),
    ],
    ids=["tool-result", "tool-call", "message"],
)
def test_rejects_event_after_final_turn(late: object) -> None:
    events = (
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="stop"),
        late,
    )
    with pytest.raises(AssertionError, match="appeared after final TurnCompleted"):
        assert_turn_invariants(events)


@pytest.mark.parametrize(
    "events",
    [
        (tool_ended("call-1"), tool_ready("call-1"), *completed_turn()),
        (*completed_turn(1), tool_ended("call-1"), tool_ready("call-1"), *completed_turn(2)),
        (
            *completed_turn(1),
            MessageStarted(turn=2),
            MessageDelta(turn=2, delta="late"),
            *completed_turn(2),
        ),
    ],
    ids=["tool-before-turns", "tool-between-turns", "message-between-turns"],
)
def test_rejects_event_outside_turn(events: tuple[object, ...]) -> None:
    with pytest.raises(AssertionError, match="appeared outside an active turn"):
        assert_turn_invariants(events)


def test_rejects_event_for_wrong_turn() -> None:
    events = (
        TurnStarted(turn=1),
        MessageStarted(turn=2),
        TurnCompleted(turn=1, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="for turn 2 appeared while in_turn was 1"):
        assert_turn_invariants(events)


def test_accepts_nonzero_initial_turn() -> None:
    events = (
        TurnStarted(turn=5),
        TurnCompleted(turn=5, outcome="completed", finish_reason="stop"),
    )
    assert_turn_invariants(events)
    assert_turn_invariants(events, initial_turn=5)


def test_accepts_retry_after_failed_turn() -> None:
    events = (
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="failed", finish_reason="error"),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    assert_turn_invariants(events)
