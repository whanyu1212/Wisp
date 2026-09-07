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

from wisp.events import (
    ErrorEvent,
    MessageCompleted,
    QueueMessageInjected,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolExecutionEnded,
    ToolExecutionStarted,
    ToolResultReady,
    TurnCompleted,
    TurnStarted,
)

_TOOL_LIFECYCLE_EVENT_TYPES = (
    ToolCallRequested,
    ToolExecutionStarted,
    ToolExecutionEnded,
    ToolResultReady,
    ToolApprovalRequested,
    ToolApprovalResolved,
)


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


def assert_tool_result_pairing(
    events: Sequence[object],
    *,
    allow_unpaired_ended_before_cancel: bool = False,
) -> None:
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

    `allow_unpaired_ended_before_cancel` covers the harness projection boundary
    where cancel is observed on `ToolExecutionEnded`: the consumer may see that
    Ended, then the cancellation error/terminal, without a following Ready.
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
    if allow_unpaired_ended_before_cancel and len(pending_ended) == 1:
        ended_index = next(iter(pending_ended.values()))[0]
        trailing = events[ended_index + 1 :]
        if (
            trailing
            and isinstance(trailing[0], ErrorEvent)
            and all(isinstance(item, (ErrorEvent, TurnCompleted)) for item in trailing)
        ):
            return
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


def assert_turn_invariants(
    events: Sequence[object],
    *,
    initial_turn: int | None = None,
) -> None:
    """Enforce strict sequential turn ordering, valid outcomes, and containment.

    Verifies:
    1. 1:1 match between TurnStarted and TurnCompleted (via assert_turn_terminals).
    2. Turn numbers advance strictly by 1 without gaps, starting at initial_turn or the first turn.
    3. Outcomes are valid ('completed', 'failed', 'cancelled').
    4. Outcome 'cancelled' requires finish_reason 'cancelled' and prohibits subsequent turns.
    5. Outcome 'completed' requires a non-cancelled finish_reason ('stop', 'tool_calls', etc.).
    6. Tool lifecycle events appear only inside an active turn.
    7. No turn or tool events appear after the final TurnCompleted.
    """

    assert_turn_terminals(events)
    last_turn_completed_idx = max(
        (idx for idx, e in enumerate(events) if isinstance(e, TurnCompleted)),
        default=None,
    )
    if last_turn_completed_idx is not None and last_turn_completed_idx < len(events) - 1:
        forbidden_trailing = [
            type(e).__name__
            for e in events[last_turn_completed_idx + 1 :]
            if isinstance(e, (TurnStarted, TurnCompleted, *_TOOL_LIFECYCLE_EVENT_TYPES))
        ]
        assert not forbidden_trailing, (
            f"Events appeared after final TurnCompleted: {', '.join(forbidden_trailing)}"
        )

    expected_turn = initial_turn
    in_turn: int | None = None
    cancelled_turn: int | None = None

    for _index, event in enumerate(events):
        if isinstance(event, TurnStarted):
            assert cancelled_turn is None, (
                f"TurnStarted for turn {event.turn} appeared after "
                f"turn {cancelled_turn} was cancelled"
            )
            if expected_turn is None:
                expected_turn = event.turn
            assert event.turn == expected_turn, (
                f"TurnStarted expected turn {expected_turn}, got {event.turn}"
            )
            in_turn = event.turn
        elif isinstance(event, TurnCompleted):
            assert in_turn == event.turn, (
                f"TurnCompleted for turn {event.turn} arrived while in_turn was {in_turn}"
            )
            assert event.outcome in ("completed", "failed", "cancelled"), (
                f"Invalid TurnCompleted outcome: {event.outcome!r}"
            )
            if event.outcome == "cancelled":
                assert event.finish_reason == "cancelled", (
                    "Cancelled turn must have finish_reason 'cancelled', "
                    f"got {event.finish_reason!r}"
                )
                cancelled_turn = event.turn
            elif event.outcome == "failed":
                assert event.finish_reason == "error", (
                    f"Failed turn must have finish_reason 'error', got {event.finish_reason!r}"
                )
            elif event.outcome == "completed":
                assert event.finish_reason not in ("error", "cancelled"), (
                    f"Completed turn must not have finish_reason {event.finish_reason!r}"
                )
            in_turn = None
            if expected_turn is not None:
                expected_turn += 1
        elif isinstance(event, _TOOL_LIFECYCLE_EVENT_TYPES):
            assert in_turn is not None, f"{type(event).__name__} appeared outside an active turn"


def assert_cancellation_settled(events: Sequence[object]) -> None:
    """Require that a cancelled run terminates cleanly with explicit settlement.

    Enforces:
    1. If a turn was interrupted, the final TurnCompleted must have outcome='cancelled'
       and finish_reason='cancelled', with an ErrorEvent before that terminal.
    2. If no turns were started, no TurnCompleted may appear, and an ErrorEvent must
       still be present.
    3. If the last turn already completed, cancellation may emit only a trailing
       ErrorEvent after that TurnCompleted (no cancelled terminal).
    4. Tool execution Ended/Ready pairing is preserved, except for a single
       unmatched Ended immediately followed by the cancellation ErrorEvent.
    """

    assert_tool_result_pairing(events, allow_unpaired_ended_before_cancel=True)
    assert_turn_terminals(events)

    turns = [e for e in events if isinstance(e, TurnCompleted)]
    started = [e for e in events if isinstance(e, TurnStarted)]
    if not started:
        assert not turns, "Cancelled run produced TurnCompleted without TurnStarted"
        error_events = [e for e in events if isinstance(e, ErrorEvent)]
        assert error_events, "Cancelled run must emit an ErrorEvent before completion"
        return

    assert turns, "Cancelled run started a turn but produced no TurnCompleted events"
    last_turn = turns[-1]
    last_idx = events.index(last_turn)
    trailing = events[last_idx + 1 :]
    trailing_errors = [event for event in trailing if isinstance(event, ErrorEvent)]

    if last_turn.outcome == "cancelled":
        assert last_turn.finish_reason == "cancelled", (
            f"Expected final turn finish_reason 'cancelled', got {last_turn.finish_reason!r}"
        )
        error_events = [event for event in events[:last_idx] if isinstance(event, ErrorEvent)]
        assert error_events, "Cancelled run must emit an ErrorEvent before completion"
        return

    if last_turn.outcome in ("completed", "failed") and trailing_errors:
        forbidden_trailing = [
            type(event).__name__
            for event in trailing
            if isinstance(event, (TurnStarted, TurnCompleted, *_TOOL_LIFECYCLE_EVENT_TYPES))
        ]
        assert not forbidden_trailing, (
            f"Events appeared after completed-turn cancellation: {', '.join(forbidden_trailing)}"
        )
        return

    raise AssertionError(f"Expected final turn outcome 'cancelled', got {last_turn.outcome!r}")


def assert_queue_ordering_invariants(
    events: Sequence[object],
    *,
    initial_steering_count: int | None = None,
    initial_follow_up_count: int | None = None,
    expected_steering: Sequence[str] | None = None,
    expected_follow_up: Sequence[str] | None = None,
) -> None:
    """Require that queue injections adhere to steering priority over follow-up and FIFO order.

    1. Injected messages cannot appear inside an active turn (TurnStarted to TurnCompleted).
    2. Within each turn transition boundary, all steering injections must precede follow-up
       injections.
    3. If initial queue counts are provided, all initial steering messages must be injected before
       any initial follow-up messages are injected across boundaries.
    4. If expected_steering / expected_follow_up snapshots are provided, injected contents for
       each kind must match that enqueue order (FIFO within kind).
    """

    in_turn = False
    injections_by_boundary: list[list[QueueMessageInjected]] = []
    current_injections: list[QueueMessageInjected] = []
    all_injections: list[QueueMessageInjected] = []

    for event in events:
        if isinstance(event, TurnStarted):
            assert not in_turn, (
                f"TurnStarted for turn {event.turn} appeared while previous turn was still active"
            )
            in_turn = True
            injections_by_boundary.append(list(current_injections))
            current_injections.clear()
        elif isinstance(event, TurnCompleted):
            assert in_turn, f"TurnCompleted for turn {event.turn} appeared outside an active turn"
            in_turn = False
        elif isinstance(event, QueueMessageInjected):
            assert not in_turn, (
                f"QueueMessageInjected ({event.kind}: {event.content!r}) appeared "
                "inside an active turn"
            )
            current_injections.append(event)
            all_injections.append(event)

    if current_injections:
        injections_by_boundary.append(list(current_injections))

    for boundary_idx, boundary_injections in enumerate(injections_by_boundary, start=1):
        saw_follow_up = False
        for injection in boundary_injections:
            if injection.kind == "follow_up":
                saw_follow_up = True
            elif injection.kind == "steering":
                assert not saw_follow_up, (
                    f"Boundary {boundary_idx}: steering injection appeared after "
                    "follow-up injection"
                )

    if initial_steering_count is not None and initial_follow_up_count is not None:
        first_follow_up_idx = next(
            (idx for idx, inj in enumerate(all_injections) if inj.kind == "follow_up"),
            None,
        )
        if first_follow_up_idx is not None:
            steering_before_follow_up = sum(
                1 for inj in all_injections[:first_follow_up_idx] if inj.kind == "steering"
            )
            assert steering_before_follow_up == min(initial_steering_count, len(all_injections)), (
                f"Expected all {initial_steering_count} initial steering messages to be injected "
                f"before follow-ups, but only {steering_before_follow_up} were"
            )

    if expected_steering is not None:
        steering_contents = [inj.content for inj in all_injections if inj.kind == "steering"]
        assert steering_contents == list(expected_steering), (
            f"Steering injections {steering_contents!r} != expected FIFO order "
            f"{list(expected_steering)!r}"
        )
    if expected_follow_up is not None:
        follow_up_contents = [inj.content for inj in all_injections if inj.kind == "follow_up"]
        assert follow_up_contents == list(expected_follow_up), (
            f"Follow-up injections {follow_up_contents!r} != expected FIFO order "
            f"{list(expected_follow_up)!r}"
        )


def assert_continuation_invariants(events: Sequence[object]) -> None:
    """Enforce provider continuation and context rebase lifecycle consistency.

    1. Any tool-bearing turn (requested tool calls or MessageCompleted.tool_calls)
       must have matching terminal tool execution results for every requested call
       (unless the turn failed or was cancelled).
    2. Any tool-bearing turn must be followed by a continuation turn (unless the run
       failed or was cancelled).
    3. Tool execution Ended/Ready pairing must hold across the entire stream.
    4. Strict turn sequencing must hold.
    """

    assert_tool_result_pairing(events)
    assert_turn_invariants(events)

    turns = [e for e in events if isinstance(e, TurnCompleted)]
    turn_starts = {e.turn: events.index(e) for e in events if isinstance(e, TurnStarted)}

    for i, completed in enumerate(turns):
        start_idx = turn_starts.get(completed.turn, 0)
        end_idx = events.index(completed)
        turn_events = events[start_idx:end_idx]

        requested_from_events = Counter(
            e.call_id for e in turn_events if isinstance(e, ToolCallRequested)
        )
        requested_from_completion = Counter(
            call.call_id
            for e in turn_events
            if isinstance(e, MessageCompleted)
            for call in e.tool_calls
        )
        requested_calls = requested_from_events | requested_from_completion

        has_tool_calls = bool(requested_calls) or completed.finish_reason == "tool_calls"
        if has_tool_calls:
            is_last = i == len(turns) - 1
            terminal_without_continuation = completed.outcome in ("cancelled", "failed")
            if is_last:
                assert terminal_without_continuation, (
                    f"Turn {completed.turn} had tool calls but has no subsequent continuation turn"
                )
            if not terminal_without_continuation:
                ended_calls = [e.call_id for e in turn_events if isinstance(e, ToolExecutionEnded)]
                if requested_calls:
                    missing = requested_calls - Counter(ended_calls)
                    assert not missing, (
                        f"Turn {completed.turn} requested calls "
                        f"{list(requested_calls.elements())} but was missing "
                        f"terminal results for: {list(missing.keys())}"
                    )
                else:
                    assert ended_calls, (
                        f"Turn {completed.turn} completed with 'tool_calls' but had no "
                        "tool execution within that turn"
                    )
