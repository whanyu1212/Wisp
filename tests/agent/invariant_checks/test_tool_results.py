"""Unit tests for assert_tool_result_pairing and assert_settled_tool_calls."""

from __future__ import annotations

import pytest

from tests.agent.invariant_checks.support import (
    completed_turn,
    tool_ended,
    tool_ready,
)
from tests.support.agent_runtime import (
    assert_settled_tool_calls,
    assert_tool_result_pairing,
    assert_turn_terminals,
)
from wisp.events import (
    ErrorEvent,
    ToolCallRequested,
    ToolExecutionStarted,
    ToolResultReady,
    TurnCompleted,
    TurnStarted,
)


def test_pairing_check_accepts_adjacent_ended_and_ready() -> None:
    events = (*completed_turn(), tool_ended(), tool_ready())
    assert_tool_result_pairing(events)
    assert_settled_tool_calls(events, ("call-1",))


def test_pairing_check_allows_requested_call_without_result() -> None:
    events = (
        TurnStarted(turn=1),
        ToolCallRequested(call_id="call-1", name="lookup", arguments={}),
        ToolExecutionStarted(call_id="call-1", name="lookup", arguments={}),
        ErrorEvent(message="Agent run cancelled"),
        TurnCompleted(turn=1, outcome="cancelled", finish_reason="cancelled"),
    )
    assert_turn_terminals(events)
    assert_tool_result_pairing(events)


def test_pairing_check_rejects_ready_without_ended() -> None:
    with pytest.raises(AssertionError, match="without ToolExecutionEnded"):
        assert_tool_result_pairing((tool_ready(),))


def test_pairing_check_rejects_ended_without_ready() -> None:
    with pytest.raises(AssertionError, match="without ToolResultReady"):
        assert_tool_result_pairing((tool_ended(),))


def test_pairing_check_allows_reused_call_id_across_rounds() -> None:
    events = (
        TurnStarted(turn=1),
        tool_ended("call-lookup-0"),
        tool_ready("call-lookup-0"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        TurnStarted(turn=2),
        tool_ended("call-lookup-0"),
        tool_ready("call-lookup-0"),
        TurnCompleted(turn=2, outcome="completed", finish_reason="tool_calls"),
    )
    assert_turn_terminals(events)
    assert_tool_result_pairing(events)
    assert_settled_tool_calls(events, ("call-lookup-0",))


def test_pairing_check_rejects_duplicate_ended() -> None:
    with pytest.raises(AssertionError, match="appeared more than once"):
        assert_tool_result_pairing((tool_ended(), tool_ended(), tool_ready()))


def test_pairing_check_rejects_nonadjacent_projection() -> None:
    with pytest.raises(AssertionError, match="must immediately follow"):
        assert_tool_result_pairing((tool_ended(), ErrorEvent(message="gap"), tool_ready()))


def test_pairing_check_rejects_mismatched_payload() -> None:
    ended = tool_ended()
    ready = ToolResultReady(
        call_id=ended.call_id,
        name=ended.name,
        output="different-output",
        is_error=ended.is_error,
    )
    with pytest.raises(AssertionError, match="does not match"):
        assert_tool_result_pairing((ended, ready))


def test_settlement_check_rejects_missing_listed_call() -> None:
    events = (*completed_turn(), tool_ended("call-1"), tool_ready("call-1"))
    with pytest.raises(AssertionError, match="missing terminal tool results"):
        assert_settled_tool_calls(events, ("call-1", "call-2"))


def test_settlement_check_counts_reused_call_id_occurrences() -> None:
    events = (*completed_turn(), tool_ended("call-lookup-0"), tool_ready("call-lookup-0"))
    with pytest.raises(AssertionError, match="missing terminal tool results"):
        assert_settled_tool_calls(events, ("call-lookup-0", "call-lookup-0"))


def test_settlement_check_accepts_matching_reused_occurrences() -> None:
    events = (
        TurnStarted(turn=1),
        tool_ended("call-lookup-0"),
        tool_ready("call-lookup-0"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        TurnStarted(turn=2),
        tool_ended("call-lookup-0"),
        tool_ready("call-lookup-0"),
        TurnCompleted(turn=2, outcome="completed", finish_reason="tool_calls"),
    )
    assert_settled_tool_calls(events, ("call-lookup-0", "call-lookup-0"))
