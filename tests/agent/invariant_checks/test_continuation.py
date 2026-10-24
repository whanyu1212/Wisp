"""Unit tests for assert_continuation_invariants on hand-built event sequences."""

from __future__ import annotations

from typing import Literal

import pytest

from tests.agent.invariant_checks.support import (
    completed_turn,
    tool_ended,
    tool_ready,
    tool_turn_end,
)
from tests.support.agent_runtime import assert_continuation_invariants
from wisp.agent.transcript_repair import INTERRUPTED_TOOL_RESULT_TEXT
from wisp.events import (
    MessageCompleted,
    ToolCallRequested,
    ToolCallSnapshot,
    ToolResultReady,
    TurnCompleted,
    TurnStarted,
)


def test_continuation_check_recognizes_truncated_tool_calls() -> None:
    events = (
        TurnStarted(turn=1),
        ToolCallRequested(call_id="call-1", name="lookup", arguments={}),
        tool_ended("call-1"),
        tool_ready("call-1"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="length"),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    assert_continuation_invariants(events)


def test_continuation_check_rejects_dropped_call() -> None:
    events = (
        TurnStarted(turn=1),
        ToolCallRequested(call_id="call-1", name="lookup", arguments={}),
        ToolCallRequested(call_id="call-2", name="lookup", arguments={}),
        tool_ended("call-1"),
        tool_ready("call-1"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="missing terminal results for: \\['call-2'\\]"):
        assert_continuation_invariants(events)


def test_continuation_check_rejects_empty_completion_call_list() -> None:
    events = (
        TurnStarted(turn=1),
        MessageCompleted(turn=1, content="", finish_reason="tool_calls", tool_calls=()),
        ToolCallRequested(call_id="call-1", name="lookup", arguments={}),
        tool_ended("call-1"),
        tool_ready("call-1"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="does not match MessageCompleted.tool_calls"):
        assert_continuation_invariants(events)


def test_continuation_check_reconciles_completed_and_requested() -> None:
    events = (
        TurnStarted(turn=1),
        MessageCompleted(
            turn=1,
            content="",
            finish_reason="tool_calls",
            tool_calls=(
                ToolCallSnapshot(call_id="call-1", name="lookup", arguments={}),
                ToolCallSnapshot(call_id="call-2", name="lookup", arguments={}),
            ),
        ),
        ToolCallRequested(call_id="call-1", name="lookup", arguments={}),
        tool_ended("call-1"),
        tool_ready("call-1"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="does not match MessageCompleted.tool_calls"):
        assert_continuation_invariants(events)


def test_continuation_check_rejects_result_for_unrequested_call() -> None:
    events = (
        TurnStarted(turn=1),
        MessageCompleted(
            turn=1,
            content="",
            finish_reason="tool_calls",
            tool_calls=(ToolCallSnapshot(call_id="call-1", name="lookup", arguments={}),),
        ),
        ToolCallRequested(call_id="call-1", name="lookup", arguments={}),
        tool_ended("call-1"),
        tool_ready("call-1"),
        tool_ended("call-2"),
        tool_ready("call-2"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="terminal results for unrequested calls"):
        assert_continuation_invariants(events)


@pytest.mark.parametrize("finish_reason", ["stop", "tool_calls"])
def test_continuation_check_rejects_results_without_requests(
    finish_reason: Literal["stop", "tool_calls"],
) -> None:
    events = (
        TurnStarted(turn=1),
        MessageCompleted(turn=1, content="", finish_reason=finish_reason, tool_calls=()),
        tool_ended("orphan"),
        tool_ready("orphan"),
        TurnCompleted(turn=1, outcome="completed", finish_reason=finish_reason),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="terminal results for unrequested calls"):
        assert_continuation_invariants(events)


def test_continuation_check_rejects_call_missing_from_completion() -> None:
    events = (
        TurnStarted(turn=1),
        MessageCompleted(
            turn=1,
            content="",
            finish_reason="tool_calls",
            tool_calls=(ToolCallSnapshot(call_id="call-1", name="lookup", arguments={}),),
        ),
        ToolCallRequested(call_id="call-1", name="lookup", arguments={}),
        ToolCallRequested(call_id="call-2", name="lookup", arguments={}),
        tool_ended("call-1"),
        tool_ready("call-1"),
        tool_ended("call-2"),
        tool_ready("call-2"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="does not match MessageCompleted.tool_calls"):
        assert_continuation_invariants(events)


def test_continuation_check_rejects_empty_tool_continuation() -> None:
    events = (
        TurnStarted(turn=1),
        MessageCompleted(turn=1, content="", finish_reason="tool_calls", tool_calls=()),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="had no tool execution within that turn"):
        assert_continuation_invariants(events)


@pytest.mark.parametrize(
    "request_event",
    [
        ToolCallRequested(call_id="call-1", name="other", arguments={"path": "a"}),
        ToolCallRequested(call_id="call-1", name="lookup", arguments={"path": "b"}),
    ],
)
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_continuation_check_rejects_changed_request_payload(
    request_event: ToolCallRequested,
    outcome: Literal["completed", "failed", "cancelled"],
) -> None:
    events = (
        TurnStarted(turn=1),
        MessageCompleted(
            turn=1,
            content="",
            finish_reason="tool_calls",
            tool_calls=(
                ToolCallSnapshot(call_id="call-1", name="lookup", arguments={"path": "a"}),
            ),
        ),
        request_event,
        tool_ended(),
        tool_ready(),
        *tool_turn_end(outcome),
    )
    with pytest.raises(AssertionError, match="names or arguments do not match"):
        assert_continuation_invariants(events)


def test_continuation_check_accepts_reordered_argument_keys() -> None:
    events = (
        TurnStarted(turn=1),
        MessageCompleted(
            turn=1,
            content="",
            finish_reason="tool_calls",
            tool_calls=(
                ToolCallSnapshot(
                    call_id="call-1", name="lookup", arguments={"query": {"a": 1, "b": 2}}
                ),
            ),
        ),
        ToolCallRequested(call_id="call-1", name="lookup", arguments={"query": {"b": 2, "a": 1}}),
        tool_ended(),
        tool_ready(),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        *completed_turn(2),
    )
    assert_continuation_invariants(events)


@pytest.mark.parametrize("include_completion", [True, False])
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_continuation_check_rejects_changed_result_name(
    include_completion: bool,
    outcome: Literal["completed", "failed", "cancelled"],
) -> None:
    completion = MessageCompleted(
        turn=1,
        content="",
        finish_reason="tool_calls",
        tool_calls=(ToolCallSnapshot(call_id="call-1", name="other", arguments={}),),
    )
    events = (
        TurnStarted(turn=1),
        *((completion,) if include_completion else ()),
        ToolCallRequested(call_id="call-1", name="other", arguments={}),
        tool_ended(),
        tool_ready(),
        *tool_turn_end(outcome),
    )
    with pytest.raises(AssertionError, match="terminal result names do not match"):
        assert_continuation_invariants(events)


def test_continuation_check_verifies_names_for_repeated_ids() -> None:
    events = (
        TurnStarted(turn=1),
        ToolCallRequested(call_id="call-1", name="lookup", arguments={}),
        ToolCallRequested(call_id="call-1", name="other", arguments={}),
        tool_ended(),
        tool_ready(),
        tool_ended(),
        tool_ready(),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        *completed_turn(2),
    )
    with pytest.raises(AssertionError, match="terminal result names do not match"):
        assert_continuation_invariants(events)


def test_continuation_check_rejects_reordered_requests() -> None:
    calls = (
        ToolCallSnapshot(call_id="call-1", name="lookup", arguments={"path": "a"}),
        ToolCallSnapshot(call_id="call-1", name="lookup", arguments={"path": "b"}),
    )
    events = (
        TurnStarted(turn=1),
        MessageCompleted(turn=1, content="", finish_reason="tool_calls", tool_calls=calls),
        *(
            ToolCallRequested(call_id=call.call_id, name=call.name, arguments=call.arguments)
            for call in reversed(calls)
        ),
        tool_ended(),
        tool_ready(),
        tool_ended(),
        tool_ready(),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        *completed_turn(2),
    )
    with pytest.raises(
        AssertionError, match="do not match MessageCompleted.tool_calls in occurrence"
    ):
        assert_continuation_invariants(events)


@pytest.mark.parametrize("request_first", [True, False])
@pytest.mark.parametrize("repeat_id", [True, False])
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_continuation_check_checks_completion_request_order(
    request_first: bool,
    repeat_id: bool,
    outcome: Literal["completed", "failed", "cancelled"],
) -> None:
    call = ToolCallSnapshot(call_id="call-1", name="lookup", arguments={})
    calls = (call, call) if repeat_id else (call,)
    requests = tuple(
        ToolCallRequested(call_id=item.call_id, name=item.name, arguments=item.arguments)
        for item in calls
    )
    events = (
        TurnStarted(turn=1),
        *(requests[:1] if request_first else ()),
        MessageCompleted(turn=1, content="", finish_reason="tool_calls", tool_calls=calls),
        *(requests[1:] if request_first else requests),
        *(event for _call in calls for event in (tool_ended(), tool_ready())),
        *tool_turn_end(outcome),
    )
    if request_first:
        with pytest.raises(
            AssertionError, match="tool request appeared before its completion snapshot"
        ):
            assert_continuation_invariants(events)
    else:
        assert_continuation_invariants(events)


@pytest.mark.parametrize("repeat_id", [True, False])
@pytest.mark.parametrize("reverse_results", [True, False])
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_continuation_check_matches_result_occurrence_order(
    repeat_id: bool,
    reverse_results: bool,
    outcome: Literal["completed", "failed", "cancelled"],
) -> None:
    second_id = "call-1" if repeat_id else "call-2"
    calls = (
        ToolCallSnapshot(call_id="call-1", name="lookup", arguments={}),
        ToolCallSnapshot(call_id=second_id, name="other", arguments={}),
    )
    results = (tool_ended(), tool_ended(second_id).model_copy(update={"name": "other"}))
    if reverse_results:
        results = results[::-1]
    events = (
        TurnStarted(turn=1),
        MessageCompleted(turn=1, content="", finish_reason="tool_calls", tool_calls=calls),
        *(
            ToolCallRequested(call_id=call.call_id, name=call.name, arguments=call.arguments)
            for call in calls
        ),
        *(
            event
            for result in results
            for event in (result, ToolResultReady.from_execution_ended(result))
        ),
        *tool_turn_end(outcome),
    )
    if reverse_results:
        with pytest.raises(AssertionError, match="terminal result names do not match"):
            assert_continuation_invariants(events)
    else:
        assert_continuation_invariants(events)


@pytest.mark.parametrize("repeat_id", [True, False])
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_continuation_check_rejects_result_before_request(
    repeat_id: bool,
    outcome: Literal["completed", "failed", "cancelled"],
) -> None:
    first_occurrence = (
        ToolCallRequested(call_id="call-1", name="lookup", arguments={}),
        tool_ended(),
        tool_ready(),
    )
    events = (
        TurnStarted(turn=1),
        *(first_occurrence if repeat_id else ()),
        tool_ended(),
        tool_ready(),
        ToolCallRequested(call_id="call-1", name="lookup", arguments={}),
        *tool_turn_end(outcome),
    )
    with pytest.raises(
        AssertionError, match="terminal result appeared before its request occurrence"
    ):
        assert_continuation_invariants(events)


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
@pytest.mark.parametrize("settled_id", [None, "call-1", "call-2"])
def test_continuation_check_allows_partial_interrupted_turn(
    outcome: Literal["failed", "cancelled"],
    settled_id: str | None,
) -> None:
    calls = tuple(
        ToolCallSnapshot(call_id=f"call-{index}", name="lookup", arguments={})
        for index in (1, 2, 3)
    )
    events = (
        TurnStarted(turn=1),
        MessageCompleted(turn=1, content="", finish_reason="tool_calls", tool_calls=calls),
        *(
            ToolCallRequested(call_id=call.call_id, name=call.name, arguments=call.arguments)
            for call in calls[:2]
        ),
        *((tool_ended(settled_id), tool_ready(settled_id)) if settled_id is not None else ()),
        *tool_turn_end(outcome),
    )
    assert_continuation_invariants(events)


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
def test_continuation_check_rejects_skipped_interrupted_call(
    outcome: Literal["failed", "cancelled"],
) -> None:
    events = (
        TurnStarted(turn=1),
        MessageCompleted(
            turn=1,
            content="",
            finish_reason="tool_calls",
            tool_calls=(
                ToolCallSnapshot(call_id="call-1", name="lookup", arguments={}),
                ToolCallSnapshot(call_id="call-2", name="lookup", arguments={}),
            ),
        ),
        ToolCallRequested(call_id="call-2", name="lookup", arguments={}),
        *tool_turn_end(outcome),
    )
    with pytest.raises(
        AssertionError, match="do not match MessageCompleted.tool_calls in occurrence"
    ):
        assert_continuation_invariants(events)


@pytest.mark.parametrize("settled_first", [True, False])
def test_continuation_check_skips_only_settled_duplicates(
    settled_first: bool,
) -> None:
    events = (
        TurnStarted(turn=1),
        MessageCompleted(
            turn=1,
            content="",
            finish_reason="tool_calls",
            tool_calls=(
                ToolCallSnapshot(call_id="repeat", name="lookup", arguments={"n": 1}),
                ToolCallSnapshot(call_id="repeat", name="lookup", arguments={"n": 2}),
                ToolCallSnapshot(call_id="middle", name="lookup", arguments={"n": 3}),
            ),
        ),
        ToolCallRequested(call_id="repeat", name="lookup", arguments={"n": 1}),
        *((tool_ended("repeat"), tool_ready("repeat")) if settled_first else ()),
        ToolCallRequested(call_id="middle", name="lookup", arguments={"n": 3}),
        *((tool_ended("repeat"), tool_ready("repeat")) if not settled_first else ()),
        tool_ended("middle"),
        tool_ready("middle"),
        *tool_turn_end("cancelled"),
    )
    if settled_first:
        assert_continuation_invariants(events)
    else:
        with pytest.raises(AssertionError, match="do not match MessageCompleted.tool_calls"):
            assert_continuation_invariants(events)


@pytest.mark.parametrize(
    ("result_order", "error"),
    [
        (("normal-2", "cancel-1"), None),
        (("cancel-1", "normal-2"), "ordinary result appeared after cancellation settlement"),
        (("cancel-1", "cancel-2"), None),
        (("cancel-2", "cancel-1"), "cancellation settlements appeared out of request order"),
    ],
)
def test_continuation_check_verifies_cancellation_settlement(
    result_order: tuple[str, str],
    error: str | None,
) -> None:
    results = {
        "normal-2": tool_ended("call-2"),
        "cancel-1": tool_ended("call-1").model_copy(
            update={
                "output": INTERRUPTED_TOOL_RESULT_TEXT,
                "is_error": True,
                "process_state": "cancelled",
            }
        ),
        "cancel-2": tool_ended("call-2").model_copy(
            update={
                "output": INTERRUPTED_TOOL_RESULT_TEXT,
                "is_error": True,
                "process_state": "cancelled",
            }
        ),
    }
    events = (
        TurnStarted(turn=1),
        ToolCallRequested(call_id="call-1", name="lookup", arguments={}),
        ToolCallRequested(call_id="call-2", name="lookup", arguments={}),
        *(
            event
            for key in result_order
            for event in (results[key], ToolResultReady.from_execution_ended(results[key]))
        ),
        *tool_turn_end("cancelled"),
    )
    if error is not None:
        with pytest.raises(AssertionError, match=error):
            assert_continuation_invariants(events)
    else:
        assert_continuation_invariants(events)


def test_continuation_check_rejects_resettled_cancelled_call() -> None:
    cancellation = tool_ended().model_copy(
        update={
            "output": INTERRUPTED_TOOL_RESULT_TEXT,
            "is_error": True,
            "process_state": "cancelled",
        }
    )
    events = (
        TurnStarted(turn=1),
        ToolCallRequested(call_id="call-1", name="lookup", arguments={}),
        ToolCallRequested(call_id="call-1", name="lookup", arguments={}),
        tool_ended(),
        tool_ready(),
        cancellation,
        ToolResultReady.from_execution_ended(cancellation),
        *tool_turn_end("cancelled"),
    )
    with pytest.raises(AssertionError, match="cancellation settled an already-settled call ID"):
        assert_continuation_invariants(events)


def test_continuation_check_allows_failed_final_tool_turn() -> None:
    events = (
        TurnStarted(turn=1),
        ToolCallRequested(call_id="call-1", name="lookup", arguments={}),
        tool_ended("call-1"),
        tool_ready("call-1"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="length"),
        TurnStarted(turn=2),
        MessageCompleted(
            turn=2,
            content="",
            finish_reason="length",
            tool_calls=(ToolCallSnapshot(call_id="call-2", name="lookup", arguments={}),),
        ),
        TurnCompleted(turn=2, outcome="failed", finish_reason="error"),
    )
    assert_continuation_invariants(events)


def test_continuation_check_accepts_tool_continuation() -> None:
    events = (
        TurnStarted(turn=1),
        tool_ended("call-1"),
        tool_ready("call-1"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    assert_continuation_invariants(events)


def test_continuation_check_rejects_unfinished_calls_at_end() -> None:
    events = (
        TurnStarted(turn=1),
        tool_ended("call-1"),
        tool_ready("call-1"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
    )
    with pytest.raises(AssertionError, match="no subsequent continuation turn"):
        assert_continuation_invariants(events)


def test_continuation_check_rejects_call_without_result() -> None:
    events = (
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="had no tool execution within that turn"):
        assert_continuation_invariants(events)
