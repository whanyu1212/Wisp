"""Assertions for agent-loop and harness lifecycle invariants.

These helpers encode compatibility contracts for later `run_agent_loop` /
`AgentHarness` extracts. They inspect a finished event list; they do not wrap
the runtime or duplicate scenario fixtures.

Turn terminals and tool terminals are split on purpose. Sequential
`ToolExecutor.execute` cancellation may emit `ToolCallRequested` (and even
`ToolExecutionStarted`) and then cancel the turn with no tool result. Prepared
batches synthesize one interrupted terminal per requested call. A single
"requested implies terminal" helper would fail real sequential-cancel streams
and pressure later slices to change production behavior.
"""

from __future__ import annotations

from collections.abc import Sequence

from wisp.events import ToolExecutionEnded, ToolResultReady, TurnCompleted, TurnStarted


def assert_turn_terminals(events: Sequence[object]) -> None:
    """Require a 1:1 match between started turns and their terminal events.

    A finished stream may start zero turns (failure or cancel before
    `TurnStarted`); that case must also omit `TurnCompleted`.
    """

    started_at: dict[int, int] = {}
    completed_at: dict[int, int] = {}
    for index, event in enumerate(events):
        if isinstance(event, TurnStarted):
            assert event.turn not in started_at, (
                f"TurnStarted for turn {event.turn} appeared more than once"
            )
            assert event.turn not in completed_at, (
                f"TurnStarted for turn {event.turn} appeared after TurnCompleted"
            )
            started_at[event.turn] = index
        elif isinstance(event, TurnCompleted):
            assert event.turn in started_at, (
                f"TurnCompleted for turn {event.turn} without a matching TurnStarted"
            )
            assert event.turn not in completed_at, f"turn {event.turn} completed more than once"
            completed_at[event.turn] = index
    missing = sorted(set(started_at) - set(completed_at))
    if not missing:
        return
    labels = ", ".join(str(turn) for turn in missing)
    noun = "turn" if len(missing) == 1 else "turns"
    raise AssertionError(f"{noun} {labels} started without a terminal TurnCompleted")


def assert_tool_result_pairing(events: Sequence[object]) -> None:
    """Require Ended/Ready pairing when a tool terminal is present.

    Does not require a terminal merely because `ToolCallRequested` or
    `ToolExecutionStarted` was emitted. Sequential cancellation may stop after
    those events; use `assert_settled_tool_calls` only on paths that promise
    settlement.
    """

    ended_at: dict[str, int] = {}
    ready_at: dict[str, int] = {}
    for index, event in enumerate(events):
        if isinstance(event, ToolExecutionEnded):
            assert event.call_id not in ended_at, (
                f"ToolExecutionEnded for {event.call_id} appeared more than once"
            )
            ended_at[event.call_id] = index
        elif isinstance(event, ToolResultReady):
            assert event.call_id not in ready_at, (
                f"ToolResultReady for {event.call_id} appeared more than once"
            )
            ready_at[event.call_id] = index
    only_ended = sorted(set(ended_at) - set(ready_at))
    only_ready = sorted(set(ready_at) - set(ended_at))
    assert not only_ended, f"ToolExecutionEnded without ToolResultReady: {', '.join(only_ended)}"
    assert not only_ready, f"ToolResultReady without ToolExecutionEnded: {', '.join(only_ready)}"
    for call_id, ended_index in ended_at.items():
        ready_index = ready_at[call_id]
        assert ready_index == ended_index + 1, (
            f"ToolResultReady for {call_id} must immediately follow ToolExecutionEnded "
            f"(ended at {ended_index}, ready at {ready_index})"
        )


def assert_settled_tool_calls(events: Sequence[object], call_ids: Sequence[str]) -> None:
    """Require an Ended/Ready pair for each listed call.

    Use for prepared-batch and truncated-batch paths that synthesize a terminal
    result per requested call. Do not use for sequential execute cancellation.
    """

    assert_tool_result_pairing(events)
    ended = {event.call_id for event in events if isinstance(event, ToolExecutionEnded)}
    missing = [call_id for call_id in call_ids if call_id not in ended]
    assert not missing, f"missing terminal tool results for call_ids: {', '.join(missing)}"
