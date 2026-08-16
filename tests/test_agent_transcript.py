from __future__ import annotations

from wisp.agent.messages import Message
from wisp.agent.transcript import (
    INTERRUPTED_TOOL_RESULT_TEXT,
    MissingToolResult,
    order_tool_result_items,
    plan_interrupted_tool_repairs,
)
from wisp.events import ToolCallSnapshot


def _assistant_with_calls(*call_ids: str) -> Message:
    return Message(
        role="assistant",
        content="",
        tool_calls=tuple(
            ToolCallSnapshot(
                call_id=call_id,
                name="read",
                arguments={"path": f"{call_id}.txt"},
            )
            for call_id in call_ids
        ),
        finish_reason="tool_calls",
    )


def test_plan_interrupted_tool_repairs_is_idempotent() -> None:
    messages = (
        Message(role="user", content="inspect files"),
        _assistant_with_calls("call-1", "call-2"),
        Message(role="user", content="historical follow-up"),
        Message(
            role="tool",
            content="first result",
            tool_call_id="call-1",
            tool_name="read",
        ),
    )

    plan = plan_interrupted_tool_repairs(messages)

    assert [(message.role, message.tool_call_id) for message in plan.messages] == [
        ("user", None),
        ("assistant", None),
        ("tool", "call-1"),
        ("tool", "call-2"),
        ("user", None),
    ]
    assert len(plan.repairs) == 1
    assert plan.repairs[0].tool_call_id == "call-2"
    assert plan.repairs[0].tool_name == "read"
    assert plan.repairs[0].content == INTERRUPTED_TOOL_RESULT_TEXT
    assert plan.repairs[0].is_error is True

    repeated = plan_interrupted_tool_repairs(plan.messages)

    assert repeated.messages == plan.messages
    assert repeated.repairs == ()


def test_plan_interrupted_tool_repairs_leaves_complete_transcript_unchanged() -> None:
    messages = (
        _assistant_with_calls("call-1"),
        Message(
            role="tool",
            content="done",
            tool_call_id="call-1",
            tool_name="read",
        ),
        Message(role="assistant", content="complete", finish_reason="stop"),
    )

    plan = plan_interrupted_tool_repairs(messages)

    assert plan.messages == messages
    assert plan.repairs == ()


def test_plan_interrupted_tool_repairs_preserves_orphan_results() -> None:
    orphan = Message(
        role="tool",
        content="legacy orphan",
        tool_call_id="orphan",
        tool_name="read",
    )

    plan = plan_interrupted_tool_repairs((orphan, _assistant_with_calls("call-1")))

    assert plan.messages[0] is orphan
    assert plan.messages[1].role == "assistant"
    assert plan.messages[2] == plan.repairs[0]
    assert plan.repairs[0].tool_call_id == "call-1"


def test_plan_interrupted_tool_repairs_handles_empty_call_id_idempotently() -> None:
    first = plan_interrupted_tool_repairs((_assistant_with_calls(""),))

    assert len(first.repairs) == 1
    assert first.repairs[0].tool_call_id == ""

    repeated = plan_interrupted_tool_repairs(first.messages)

    assert repeated.messages == first.messages
    assert repeated.repairs == ()


def test_plan_interrupted_tool_repairs_consumes_reused_call_ids_per_occurrence() -> None:
    first_result = Message(
        role="tool",
        content="first completed",
        tool_call_id="call-1",
        tool_name="read",
    )
    messages = (
        _assistant_with_calls("call-1"),
        first_result,
        Message(role="user", content="try again"),
        _assistant_with_calls("call-1"),
    )

    plan = plan_interrupted_tool_repairs(messages)

    assert [(message.role, message.tool_call_id) for message in plan.messages] == [
        ("assistant", None),
        ("tool", "call-1"),
        ("user", None),
        ("assistant", None),
        ("tool", "call-1"),
    ]
    assert plan.messages[1] is first_result
    assert len(plan.repairs) == 1
    assert plan.messages[-1] is plan.repairs[0]
    assert plan.repairs[0].content == INTERRUPTED_TOOL_RESULT_TEXT

    repeated = plan_interrupted_tool_repairs(plan.messages)

    assert repeated.messages == plan.messages
    assert repeated.repairs == ()


def test_order_tool_result_items_is_public_and_marks_missing_results() -> None:
    # Regression for #358: sessions.replay depends on this ordering primitive
    # directly, so it must be importable as a public name.
    result = Message(
        role="tool",
        content="done",
        tool_call_id="call-1",
        tool_name="read",
    )
    messages = (_assistant_with_calls("call-1", "call-2"), result)

    ordered = order_tool_result_items(messages, message_of=lambda message: message)

    assert ordered[0] is messages[0]
    assert ordered[1] is result
    assert isinstance(ordered[2], MissingToolResult)
    assert ordered[2].tool_call.call_id == "call-2"


def test_plan_interrupted_tool_repairs_matches_reused_id_to_nearest_call() -> None:
    later_result = Message(
        role="tool",
        content="later completed",
        tool_call_id="call-1",
        tool_name="read",
    )
    messages = (
        _assistant_with_calls("call-1"),
        Message(role="user", content="try again"),
        _assistant_with_calls("call-1"),
        later_result,
    )

    plan = plan_interrupted_tool_repairs(messages)

    assert len(plan.repairs) == 1
    assert plan.messages[1] is plan.repairs[0]
    assert plan.messages[-1] is later_result
    assert [(message.role, message.tool_call_id) for message in plan.messages] == [
        ("assistant", None),
        ("tool", "call-1"),
        ("user", None),
        ("assistant", None),
        ("tool", "call-1"),
    ]
