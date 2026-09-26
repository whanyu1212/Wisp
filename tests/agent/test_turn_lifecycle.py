from __future__ import annotations

from typing import Any, Literal

import pytest

from wisp.agent.turn_lifecycle import CANCELLED_RUN_MESSAGE, TurnLifecycle
from wisp.events import (
    FinishReason,
    MessageCompleted,
    ToolCallSnapshot,
    TurnCompleted,
    TurnStarted,
    WispEvent,
)


def _payloads(*events: WispEvent) -> list[dict[str, Any]]:
    return [event.model_dump(exclude={"timestamp"}) for event in events]


def test_start_rejects_a_second_open_turn() -> None:
    lifecycle = TurnLifecycle()
    assert _payloads(lifecycle.start(1)) == [{"type": "turn.started", "turn": 1}]

    with pytest.raises(RuntimeError, match="turn 1 is still open"):
        lifecycle.start(2)

    assert (lifecycle.open_turn, lifecycle.latest_turn) == (1, 1)


def test_complete_closes_the_turn_exactly_once() -> None:
    lifecycle = TurnLifecycle()
    lifecycle.start(3)

    completed = lifecycle.complete("completed", "stop")

    assert (completed.turn, completed.outcome, completed.finish_reason) == (3, "completed", "stop")
    assert (lifecycle.open_turn, lifecycle.latest_turn) == (None, 3)
    with pytest.raises(RuntimeError, match="No open turn"):
        lifecycle.complete("failed", "error")


@pytest.mark.parametrize(
    ("outcome", "finish_reason"),
    [("failed", "error"), ("cancelled", "cancelled")],
)
def test_terminal_events_close_an_open_turn(
    outcome: Literal["failed", "cancelled"], finish_reason: FinishReason
) -> None:
    lifecycle = TurnLifecycle()
    lifecycle.start(2)

    events = lifecycle.terminal_events("boom", outcome=outcome)

    assert _payloads(*events) == [
        {"type": "error", "message": "boom"},
        {
            "type": "turn.completed",
            "turn": 2,
            "outcome": outcome,
            "finish_reason": finish_reason,
        },
    ]
    assert lifecycle.open_turn is None
    # A later terminal for the same run carries only the error.
    assert _payloads(*lifecycle.terminal_events("again", outcome="failed")) == [
        {"type": "error", "message": "again"}
    ]


def test_cancelled_events_without_a_turn_carry_only_the_error() -> None:
    lifecycle = TurnLifecycle()

    assert _payloads(*lifecycle.cancelled_events()) == [
        {"type": "error", "message": CANCELLED_RUN_MESSAGE}
    ]
    assert lifecycle.latest_turn == 0


def test_observe_tracks_events_and_resets_tool_calls_per_turn() -> None:
    lifecycle = TurnLifecycle()
    tool_call = ToolCallSnapshot(call_id="call-1", name="read", arguments={})

    lifecycle.observe(TurnStarted(turn=5))
    lifecycle.observe(
        MessageCompleted(turn=5, content="", finish_reason="tool_calls", tool_calls=(tool_call,))
    )
    assert (lifecycle.open_turn, lifecycle.had_tool_calls) == (5, True)

    lifecycle.observe(TurnCompleted(turn=5, outcome="completed", finish_reason="tool_calls"))
    # The completed turn's tool state stays readable until the next turn starts.
    assert (lifecycle.open_turn, lifecycle.had_tool_calls) == (None, True)

    lifecycle.observe(TurnStarted(turn=6))
    assert (lifecycle.open_turn, lifecycle.latest_turn, lifecycle.had_tool_calls) == (6, 6, False)


def test_observe_counts_only_requested_tool_calls() -> None:
    lifecycle = TurnLifecycle()
    lifecycle.observe(TurnStarted(turn=1))

    # The loop runs a tool batch only for requested calls, whatever the finish reason says.
    lifecycle.observe(MessageCompleted(turn=1, content="", finish_reason="tool_calls"))

    assert lifecycle.had_tool_calls is False
