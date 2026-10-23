"""Unit tests for assert_message_pairing on hand-built event sequences."""

from __future__ import annotations

from typing import Literal

import pytest

from tests.support.agent_runtime import assert_message_pairing
from wisp.events import (
    ErrorEvent,
    MessageCompleted,
    MessageDelta,
    MessageStarted,
    TurnCompleted,
    TurnStarted,
)


def _turn_end(turn: int, outcome: Literal["completed", "failed", "cancelled"]) -> TurnCompleted:
    finish_reason = {"completed": "stop", "failed": "error", "cancelled": "cancelled"}[outcome]
    return TurnCompleted(turn=turn, outcome=outcome, finish_reason=finish_reason)


def _completed(turn: int, finish_reason: str = "stop") -> MessageCompleted:
    return MessageCompleted(turn=turn, content="reply", finish_reason=finish_reason)


def test_message_check_accepts_each_turns_message_closed_once() -> None:
    assert_message_pairing(
        (
            TurnStarted(turn=1),
            MessageStarted(turn=1),
            MessageDelta(turn=1, delta="reply"),
            _completed(1, "tool_calls"),
            _turn_end(1, "completed"),
            TurnStarted(turn=2),
            MessageStarted(turn=2),
            _completed(2),
            _turn_end(2, "completed"),
        )
    )


def test_message_check_accepts_a_failed_turn_whose_message_closed_as_an_error() -> None:
    assert_message_pairing(
        (
            TurnStarted(turn=1),
            MessageStarted(turn=1),
            _completed(1, "error"),
            ErrorEvent(message="connection reset"),
            _turn_end(1, "failed"),
        )
    )


def test_message_check_accepts_a_turn_that_failed_before_opening_a_message() -> None:
    assert_message_pairing(
        (TurnStarted(turn=1), ErrorEvent(message="connection reset"), _turn_end(1, "failed"))
    )


def test_message_check_accepts_a_cancelled_turn_with_its_message_open() -> None:
    assert_message_pairing((TurnStarted(turn=1), MessageStarted(turn=1), _turn_end(1, "cancelled")))


@pytest.mark.parametrize("outcome", ["completed", "failed"])
def test_message_check_rejects_a_turn_ending_with_its_message_open(
    outcome: Literal["completed", "failed"],
) -> None:
    with pytest.raises(AssertionError, match=f"ended '{outcome}' with its message still open"):
        assert_message_pairing((TurnStarted(turn=1), MessageStarted(turn=1), _turn_end(1, outcome)))


def test_message_check_rejects_a_completion_without_a_start() -> None:
    with pytest.raises(AssertionError, match="without an open MessageStarted"):
        assert_message_pairing((TurnStarted(turn=1), _completed(1), _turn_end(1, "completed")))


def test_message_check_rejects_a_second_completion() -> None:
    with pytest.raises(AssertionError, match="without an open MessageStarted"):
        assert_message_pairing(
            (
                TurnStarted(turn=1),
                MessageStarted(turn=1),
                _completed(1),
                _completed(1),
                _turn_end(1, "completed"),
            )
        )


def test_message_check_rejects_a_start_while_a_message_is_open() -> None:
    with pytest.raises(AssertionError, match="was still open"):
        assert_message_pairing(
            (TurnStarted(turn=1), MessageStarted(turn=1), MessageStarted(turn=1))
        )


def test_message_check_rejects_a_stream_ending_with_its_message_open() -> None:
    with pytest.raises(AssertionError, match="was never completed"):
        assert_message_pairing((TurnStarted(turn=1), MessageStarted(turn=1)))
