"""Provider-neutral transcript repair helpers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from wisp.agent.messages import Message

INTERRUPTED_TOOL_RESULT_TEXT = (
    "Tool call interrupted before completion; execution outcome is unknown."
)


@dataclass(frozen=True, slots=True)
class TranscriptRepairPlan:
    """Logical transcript plus newly synthesized messages that require persistence."""

    messages: tuple[Message, ...]
    repairs: tuple[Message, ...]


def plan_interrupted_tool_repairs(messages: Sequence[Message]) -> TranscriptRepairPlan:
    """Pair tool results with calls and synthesize errors for missing results.

    Persisted sessions remain append-only, so an older repair may physically appear
    after later messages. The returned logical transcript moves each matched result
    next to its assistant call without rewriting the durable audit log.
    """

    source = tuple(messages)
    result_index_by_call_id: dict[str, int] = {}
    referenced_call_ids: set[str] = set()

    for index, message in enumerate(source):
        if message.role == "tool" and message.tool_call_id is not None:
            result_index_by_call_id.setdefault(message.tool_call_id, index)
        if message.role == "assistant" and message.tool_calls:
            referenced_call_ids.update(call.call_id for call in message.tool_calls)

    matched_result_indices = {
        result_index_by_call_id[call_id]
        for call_id in referenced_call_ids
        if call_id in result_index_by_call_id
    }
    repaired: list[Message] = []
    repairs: list[Message] = []
    handled_call_ids: set[str] = set()

    for index, message in enumerate(source):
        if index in matched_result_indices:
            continue
        repaired.append(message)
        if message.role != "assistant" or not message.tool_calls:
            continue

        for tool_call in message.tool_calls:
            if tool_call.call_id in handled_call_ids:
                continue
            handled_call_ids.add(tool_call.call_id)
            result_index = result_index_by_call_id.get(tool_call.call_id)
            if result_index is not None:
                repaired.append(source[result_index])
                continue

            repair = Message(
                role="tool",
                content=INTERRUPTED_TOOL_RESULT_TEXT,
                tool_call_id=tool_call.call_id,
                tool_name=tool_call.name,
                is_error=True,
            )
            repaired.append(repair)
            repairs.append(repair)

    return TranscriptRepairPlan(messages=tuple(repaired), repairs=tuple(repairs))


__all__ = [
    "INTERRUPTED_TOOL_RESULT_TEXT",
    "TranscriptRepairPlan",
    "plan_interrupted_tool_repairs",
]
