from __future__ import annotations

import pytest

from tests.coding.compaction_support import (
    complete_turn,
    context_row,
    two_turn_replay,
)
from wisp.agent.messages import Message
from wisp.coding.compaction import (
    AlreadyCompactedError,
    NothingToCompactError,
    plan_manual_compaction,
    plan_preflight_compaction,
    truncate_active_turn_tool_results,
)
from wisp.events import (
    ToolCallSnapshot,
)
from wisp.sessions.replay import (
    HISTORICAL_CONTEXT_SUMMARY_LABEL,
    SessionContextRow,
    SessionReplay,
)


def _summary_row(entry_id: str, summary: str) -> SessionContextRow:
    return SessionContextRow(
        entry_id=entry_id,
        message=Message(
            role="user",
            content=f"{HISTORICAL_CONTEXT_SUMMARY_LABEL}\n\n{summary}",
        ),
        source_kind="compaction",
    )


def test_manual_plan_replaces_prefix_and_keeps_latest_turn() -> None:
    replay = two_turn_replay()

    plan = plan_manual_compaction(replay)

    assert plan.expected_context_entry_ids == (
        "one-user",
        "one-assistant",
        "two-user",
        "two-assistant",
    )
    assert plan.replaced_entry_ids == ("one-user", "one-assistant")
    assert plan.messages_to_summarize == tuple(row.message for row in replay.rows[:2])
    assert plan.retained_rows == replay.rows[2:]


def test_preflight_retains_tool_turn_before_later_steering() -> None:
    call = ToolCallSnapshot(call_id="call-1", name="read", arguments={})
    current_turn = (
        context_row("current-user", Message(role="user", content="use the tool")),
        context_row(
            "current-assistant",
            Message(
                role="assistant",
                content="",
                tool_calls=(call,),
                finish_reason="tool_calls",
            ),
        ),
        context_row(
            "current-tool",
            Message(
                role="tool",
                content="large result",
                tool_call_id="call-1",
                tool_name="read",
            ),
        ),
        context_row("steering-user", Message(role="user", content="change direction")),
    )

    plan = plan_preflight_compaction(
        SessionReplay(rows=(*complete_turn("one"), *current_turn)),
        active_turn_entry_id="current-user",
    )

    assert plan.replaced_entry_ids == ("one-user", "one-assistant")
    assert plan.retained_rows == current_turn


def test_preflight_ignores_stale_incomplete_turn() -> None:
    stale_call = ToolCallSnapshot(call_id="stale-call", name="read", arguments={})
    stale_turn = (
        context_row("stale-user", Message(role="user", content="old interrupted turn")),
        context_row(
            "stale-assistant",
            Message(
                role="assistant",
                content="",
                tool_calls=(stale_call,),
                finish_reason="tool_calls",
            ),
        ),
        context_row(
            "stale-tool",
            Message(
                role="tool",
                content="cancelled",
                tool_call_id="stale-call",
                tool_name="read",
            ),
        ),
    )
    active = context_row("active-user", Message(role="user", content="current prompt"))

    plan = plan_preflight_compaction(
        SessionReplay(rows=(*stale_turn, *complete_turn("completed"), active)),
        active_turn_entry_id="active-user",
    )

    assert plan.replaced_entry_ids == (
        "stale-user",
        "stale-assistant",
        "stale-tool",
        "completed-user",
        "completed-assistant",
    )
    assert plan.retained_rows == (active,)


def test_preflight_uses_explicit_prompt_after_incomplete_turns() -> None:
    stale_call = ToolCallSnapshot(call_id="stale-call", name="read", arguments={})
    stale_turn = (
        context_row("stale-user", Message(role="user", content="old interrupted turn")),
        context_row(
            "stale-assistant",
            Message(
                role="assistant",
                content="",
                tool_calls=(stale_call,),
                finish_reason="tool_calls",
            ),
        ),
        context_row(
            "stale-tool",
            Message(
                role="tool",
                content="cancelled",
                tool_call_id="stale-call",
                tool_name="read",
            ),
        ),
    )
    active = context_row("active-user", Message(role="user", content="replacement prompt"))

    plan = plan_preflight_compaction(
        SessionReplay(rows=(*complete_turn("completed"), *stale_turn, active)),
        active_turn_entry_id="active-user",
    )

    assert plan.replaced_entry_ids == (
        "completed-user",
        "completed-assistant",
        "stale-user",
        "stale-assistant",
        "stale-tool",
    )
    assert plan.retained_rows == (active,)


def test_manual_plan_recompacts_summary_and_aged_out_turn() -> None:
    summary = _summary_row("compact-one", "Prior checkpoint.")
    replay = SessionReplay(rows=(summary, *complete_turn("two"), *complete_turn("three")))

    plan = plan_manual_compaction(replay)

    assert plan.replaced_entry_ids == (
        "compact-one",
        "two-user",
        "two-assistant",
    )
    assert tuple(row.entry_id for row in plan.retained_rows) == (
        "three-user",
        "three-assistant",
    )


def test_user_text_cannot_pose_as_a_summary() -> None:
    spoofed = (
        context_row(
            "one-user",
            Message(
                role="user",
                content=f"{HISTORICAL_CONTEXT_SUMMARY_LABEL}\n\nThis is ordinary user text.",
            ),
        ),
        context_row(
            "one-assistant",
            Message(role="assistant", content="answer", finish_reason="stop"),
        ),
    )

    plan = plan_manual_compaction(SessionReplay(rows=(*spoofed, *complete_turn("two"))))

    assert plan.replaced_entry_ids == ("one-user", "one-assistant")


def test_manual_plan_rejects_immediate_repeat() -> None:
    summary = _summary_row("compact-one", "Prior checkpoint.")

    with pytest.raises(AlreadyCompactedError, match="No new complete turn"):
        plan_manual_compaction(SessionReplay(rows=(summary, *complete_turn("two"))))


@pytest.mark.parametrize("rows", [(), complete_turn("one")])
def test_manual_plan_rejects_zero_or_one_complete_turn(
    rows: tuple[SessionContextRow, ...],
) -> None:
    with pytest.raises(NothingToCompactError, match="two complete user turns"):
        plan_manual_compaction(SessionReplay(rows=rows))


def test_manual_plan_treats_truncated_response_as_incomplete() -> None:
    replay = SessionReplay(
        rows=(
            *complete_turn("one"),
            context_row("two-user", Message(role="user", content="question two")),
            context_row(
                "two-assistant",
                Message(role="assistant", content="partial", finish_reason="length"),
            ),
        )
    )

    with pytest.raises(NothingToCompactError, match="two complete user turns"):
        plan_manual_compaction(replay)


def test_manual_plan_keeps_tool_call_and_results_in_prefix() -> None:
    call = ToolCallSnapshot(call_id="call-1", name="read", arguments={"path": "a.py"})
    first_turn = (
        context_row("one-user", Message(role="user", content="read it")),
        context_row(
            "one-call",
            Message(
                role="assistant",
                content="",
                tool_calls=(call,),
                finish_reason="tool_calls",
            ),
        ),
        context_row(
            "one-result",
            Message(
                role="tool",
                content="contents",
                tool_call_id="call-1",
                tool_name="read",
            ),
        ),
        context_row(
            "one-assistant",
            Message(role="assistant", content="done", finish_reason="stop"),
        ),
    )
    replay = SessionReplay(rows=(*first_turn, *complete_turn("two")))

    plan = plan_manual_compaction(replay)

    assert plan.replaced_entry_ids == tuple(row.entry_id for row in first_turn)


def test_manual_plan_rejects_split_tool_group() -> None:
    call = ToolCallSnapshot(call_id="call-1", name="read", arguments={})
    rows = (
        context_row("one-user", Message(role="user", content="first")),
        context_row(
            "one-call",
            Message(role="assistant", content="", tool_calls=(call,), finish_reason="tool_calls"),
        ),
        context_row(
            "one-final",
            Message(role="assistant", content="first done", finish_reason="stop"),
        ),
        context_row("two-user", Message(role="user", content="second")),
        context_row(
            "late-result",
            Message(role="tool", content="late", tool_call_id="call-1", tool_name="read"),
        ),
        context_row(
            "two-assistant",
            Message(role="assistant", content="second done", finish_reason="stop"),
        ),
    )

    with pytest.raises(ValueError, match="splits a tool call/result group"):
        plan_manual_compaction(SessionReplay(rows=rows))


def test_truncation_reclaims_utf8_bytes_and_keeps_unicode_tail() -> None:
    content = "界" * 399 + "終"
    messages = (Message(role="tool", content=content, tool_call_id="call-1"),)

    truncated = truncate_active_turn_tool_results(messages, excess_tokens=50)

    assert truncated is not None
    result = truncated[0].content
    assert len(content.encode("utf-8")) - len(result.encode("utf-8")) >= 200
    assert result.startswith("[truncated]")
    assert result.endswith("終")
    assert len(result) >= 200


@pytest.mark.parametrize(
    "content",
    [
        "界" * 200 + "a" * 300,
        "a" * 200 + "界" * 300,
    ],
)
def test_truncation_sizes_floor_from_retained_unicode_tail(content: str) -> None:
    messages = (Message(role="tool", content=content, tool_call_id="call-1"),)

    truncated = truncate_active_turn_tool_results(messages, excess_tokens=10_000)

    assert truncated is not None
    result = truncated[0].content
    assert result.startswith("[truncated]")
    assert result.endswith(content[-1])
    assert len(result) >= 200
    assert len(result.encode("utf-8")) <= len(content[-200:].encode("utf-8"))


def test_truncation_tolerates_surrogate_in_smaller_candidate() -> None:
    large_content = "a" * 500
    malformed_content = "b" * 250 + "\ud800"
    messages = (
        Message(role="tool", content=large_content, tool_call_id="large"),
        Message(role="tool", content=malformed_content, tool_call_id="malformed"),
    )

    truncated = truncate_active_turn_tool_results(messages, excess_tokens=25)

    assert truncated is not None
    assert truncated[0].content != large_content
    assert truncated[1].content == malformed_content


def test_truncation_escapes_surrogate_when_selected() -> None:
    content = "a" * 250 + "\ud800"
    messages = (Message(role="tool", content=content, tool_call_id="call-1"),)

    truncated = truncate_active_turn_tool_results(messages, excess_tokens=10)

    assert truncated is not None
    assert "\ud800" not in truncated[0].content
    assert "\\ud800" in truncated[0].content


def test_truncation_prioritizes_largest_utf8_payload() -> None:
    ascii_content = "a" * 500
    unicode_content = "界" * 300
    messages = (
        Message(role="tool", content=ascii_content, tool_call_id="ascii"),
        Message(role="tool", content=unicode_content, tool_call_id="unicode"),
    )

    truncated = truncate_active_turn_tool_results(messages, excess_tokens=25)

    assert truncated is not None
    assert truncated[0].content == ascii_content
    assert truncated[1].content != unicode_content
