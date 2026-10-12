"""Event builders shared by the runtime invariant checker tests."""

from __future__ import annotations

from typing import Literal

from wisp.events import (
    ErrorEvent,
    ToolExecutionEnded,
    ToolResultReady,
    TurnCompleted,
    TurnStarted,
)


def completed_turn(turn: int = 1) -> tuple[object, ...]:
    return (
        TurnStarted(turn=turn),
        TurnCompleted(turn=turn, outcome="completed", finish_reason="stop"),
    )


def tool_ended(call_id: str = "call-1") -> ToolExecutionEnded:
    return ToolExecutionEnded(
        call_id=call_id,
        name="lookup",
        output=f"output-{call_id}",
        is_error=False,
    )


def tool_ready(call_id: str = "call-1") -> ToolResultReady:
    return ToolResultReady.from_execution_ended(tool_ended(call_id))


def tool_turn_end(outcome: Literal["completed", "failed", "cancelled"]) -> tuple[object, ...]:
    if outcome == "completed":
        return (
            TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
            *completed_turn(2),
        )
    return (
        ErrorEvent(message="Tool execution interrupted"),
        TurnCompleted(
            turn=1, outcome=outcome, finish_reason="error" if outcome == "failed" else "cancelled"
        ),
    )
