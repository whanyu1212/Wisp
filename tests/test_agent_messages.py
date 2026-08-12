"""Unit tests for provider-facing message normalization.

``provider_history_message`` decides how a durable transcript message is replayed
to the provider. Rows from a completed turn are rewritten into narrated history
(the model's own past tool calls are dropped or flattened, and tool results become
labelled "historical observation" user text) so that replayed content can never be
mistaken for a live instruction. That rewrite is destructive: it discards the
assistant-authored ``tool_calls`` structure and the paired tool-role result.

Rows still inside the *active* turn must NOT go through that rewrite. When
mid-turn compaction forces a transcript rebuild, active-turn rows are replayed
again in the same request — they need to survive as the model's own structured
tool calls, or the model has no record of having made them and repeats the work.

These tests currently fail because ``provider_history_message`` (and its
sequence-level caller) has no way to know which rows are "active" — every row is
narrated identically regardless of turn boundary.
"""

from __future__ import annotations

from wisp.agent.messages import Message, normalize_provider_history
from wisp.events import ToolCallSnapshot


def _call(call_id: str = "call-1") -> ToolCallSnapshot:
    return ToolCallSnapshot(call_id=call_id, name="lookup", arguments={"query": "wisp"})


def test_active_turn_tool_call_and_result_survive_structured() -> None:
    """Rows at/after the active boundary keep native tool-call/result shape."""

    transcript = (
        Message(role="user", content="previous turn"),
        Message(role="assistant", content="", tool_calls=(_call(),)),
        Message(role="tool", content="found it", tool_name="lookup", tool_call_id="call-1"),
        Message(role="assistant", content="checking now", tool_calls=(_call("call-2"),)),
        Message(role="tool", content="second result", tool_name="lookup", tool_call_id="call-2"),
    )

    # Only the last two rows (index >= 3) belong to the still-running turn.
    normalized = normalize_provider_history(transcript, active_from=3)

    assert [(m.role, m.content) for m in normalized] == [
        ("user", "previous turn"),
        # Historical assistant tool-call row with empty content is dropped; its
        # paired result is narrated as a historical observation, same as today.
        (
            "user",
            "[Historical tool observation — not a user instruction]\n"
            "Tool: lookup (call-1)\n\n"
            "found it",
        ),
        ("assistant", "checking now"),
        ("tool", "second result"),
    ]
    active_assistant = normalized[2]
    assert active_assistant.tool_calls is not None
    assert [c.call_id for c in active_assistant.tool_calls] == ["call-2"]
    active_tool_result = normalized[3]
    assert active_tool_result.role == "tool"
    assert active_tool_result.tool_call_id == "call-2"


def test_historical_rows_before_boundary_are_still_narrated() -> None:
    """Rows before the active boundary keep today's lossy, safe rewrite.

    A historical tool result must read as an untrusted, labelled observation
    rather than a native ``tool``-role message. Its paired assistant row must lose
    its ``tool_calls`` regardless of whether it also carries text — a historical
    assistant message with nonblank content ("I'll check that...") plus intact
    ``tool_calls`` but a narrated (non-``tool``-role) paired result is a malformed
    request: a structured function call with no matching function output.
    """

    transcript = (
        Message(role="assistant", content="checking now", tool_calls=(_call(),)),
        Message(role="tool", content="found it", tool_name="lookup", tool_call_id="call-1"),
    )

    normalized = normalize_provider_history(transcript, active_from=2)

    assert normalized[0].role == "assistant"
    assert not normalized[0].tool_calls
    assert normalized[1].role == "user"
    assert normalized[1].content.startswith(
        "[Historical tool observation — not a user instruction]"
    )


def test_no_boundary_preserves_fully_narrated_behavior() -> None:
    """Without an active boundary, every row is narrated (today's cross-turn path)."""

    transcript = (
        Message(role="assistant", content="checking now", tool_calls=(_call(),)),
        Message(role="tool", content="found it", tool_name="lookup", tool_call_id="call-1"),
    )

    normalized = normalize_provider_history(transcript, active_from=None)

    assert not normalized[0].tool_calls
    assert normalized[1].role == "user"
    assert normalized[1].content.startswith(
        "[Historical tool observation — not a user instruction]"
    )
