"""Unit tests for assert_queue_ordering_invariants on hand-built event sequences."""

from __future__ import annotations

import pytest

from tests.agent.invariant_checks.support import completed_turn
from tests.support.agent_runtime import assert_queue_ordering_invariants
from wisp.events import (
    QueueMessageInjected,
    TurnCompleted,
    TurnStarted,
)


def test_queue_order_check_rejects_priority_reversal() -> None:
    events = (
        QueueMessageInjected(kind="follow_up", content="follow 1"),
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="stop"),
        QueueMessageInjected(kind="steering", content="steer 2"),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="Expected at least 1 initial steering messages"):
        assert_queue_ordering_invariants(
            events,
            initial_steering_count=1,
            initial_follow_up_count=1,
        )


def test_queue_order_check_accepts_new_steering_before_follow_up() -> None:
    events = (
        *completed_turn(1),
        QueueMessageInjected(kind="steering", content="initial steer"),
        *completed_turn(2),
        QueueMessageInjected(kind="steering", content="dynamic steer"),
        *completed_turn(3),
        QueueMessageInjected(kind="follow_up", content="initial follow"),
        *completed_turn(4),
    )
    assert_queue_ordering_invariants(
        events,
        initial_steering_count=1,
        initial_follow_up_count=1,
    )


def test_queue_order_check_accepts_steering_before_follow_up() -> None:
    events = (
        QueueMessageInjected(kind="steering", content="steer text"),
        QueueMessageInjected(kind="follow_up", content="follow text"),
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="stop"),
    )
    assert_queue_ordering_invariants(events)


def test_queue_order_check_rejects_follow_up_before_steering() -> None:
    events = (
        QueueMessageInjected(kind="follow_up", content="follow text"),
        QueueMessageInjected(kind="steering", content="steer text"),
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="[Ss]teering injection.*appeared after follow-up"):
        assert_queue_ordering_invariants(events)


def test_queue_order_check_rejects_injection_mid_turn() -> None:
    events = (
        TurnStarted(turn=1),
        QueueMessageInjected(kind="steering", content="in flight"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="appeared inside an active turn"):
        assert_queue_ordering_invariants(events)


def test_queue_order_check_rejects_within_kind_reorder() -> None:
    events = (
        QueueMessageInjected(kind="steering", content="steer 2"),
        QueueMessageInjected(kind="steering", content="steer 1"),
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="expected FIFO order"):
        assert_queue_ordering_invariants(events, expected_steering=("steer 1", "steer 2"))


def test_queue_order_check_accepts_matching_fifo_snapshot() -> None:
    events = (
        QueueMessageInjected(kind="steering", content="steer 1"),
        QueueMessageInjected(kind="steering", content="steer 2"),
        QueueMessageInjected(kind="follow_up", content="follow 1"),
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="stop"),
    )
    assert_queue_ordering_invariants(
        events,
        expected_steering=("steer 1", "steer 2"),
        expected_follow_up=("follow 1",),
    )
