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

from collections import Counter
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
    """Require Ended/Ready pairing for each tool execution occurrence.

    Uniqueness is per unmatched occurrence, not per run. Gemini may omit
    function-call IDs; the Google adapter then derives `call-{name}-{index}`
    from the current response, so later rounds can legitimately reuse a
    `call_id` after the previous pair has closed.

    Ready must be a payload projection of its Ended event: shared result
    fields (`name`, `output`, `is_error`, failure/process metadata) match.
    The harness records Ended in the transcript while continuation consumes
    Ready, so ID-and-adjacency-only pairing would miss divergent histories.

    Does not require a terminal merely because `ToolCallRequested` or
    `ToolExecutionStarted` was emitted. Sequential cancellation may stop after
    those events; use `assert_settled_tool_calls` only on paths that promise
    settlement.
    """

    pending_ended: dict[str, tuple[int, ToolExecutionEnded]] = {}
    for index, event in enumerate(events):
        if isinstance(event, ToolExecutionEnded):
            assert event.call_id not in pending_ended, (
                f"ToolExecutionEnded for {event.call_id} appeared more than once "
                "before its ToolResultReady"
            )
            pending_ended[event.call_id] = (index, event)
        elif isinstance(event, ToolResultReady):
            assert event.call_id in pending_ended, (
                f"ToolResultReady without ToolExecutionEnded: {event.call_id}"
            )
            ended_index, ended = pending_ended.pop(event.call_id)
            assert index == ended_index + 1, (
                f"ToolResultReady for {event.call_id} must immediately follow "
                f"ToolExecutionEnded (ended at {ended_index}, ready at {index})"
            )
            ended_payload = ended._result_payload()
            ready_payload = event._result_payload()
            if ended_payload != ready_payload:
                mismatched = ", ".join(
                    sorted(
                        key
                        for key in ended_payload.keys() | ready_payload.keys()
                        if ended_payload.get(key) != ready_payload.get(key)
                    )
                )
                raise AssertionError(
                    f"ToolResultReady payload for {event.call_id} does not match "
                    f"ToolExecutionEnded ({mismatched})"
                )
    unmatched = sorted(pending_ended)
    assert not unmatched, f"ToolExecutionEnded without ToolResultReady: {', '.join(unmatched)}"


def assert_settled_tool_calls(events: Sequence[object], call_ids: Sequence[str]) -> None:
    """Require an Ended/Ready pair for each listed call occurrence.

    `call_ids` is a bag, not a set: two entries with the same fallback ID
    require two Ended/Ready pairs. Use for prepared-batch and truncated-batch
    paths that synthesize a terminal result per requested call. Do not use for
    sequential execute cancellation.
    """

    assert_tool_result_pairing(events)
    ended_counts = Counter(
        event.call_id for event in events if isinstance(event, ToolExecutionEnded)
    )
    expected_counts = Counter(call_ids)
    missing = [
        f"{call_id} ({ended_counts[call_id]}/{expected})"
        for call_id, expected in expected_counts.items()
        if ended_counts[call_id] < expected
    ]
    assert not missing, f"missing terminal tool results for call_ids: {', '.join(missing)}"
