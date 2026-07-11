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
    result_index_by_call_occurrence: dict[tuple[int, int], int] = {}
    pending_call_occurrences: dict[str, list[tuple[int, int]]] = {}
    matched_result_indices: set[int] = set()

    for message_index, message in enumerate(source):
        if message.role == "assistant" and message.tool_calls:
            for call_index, tool_call in enumerate(message.tool_calls):
                pending_call_occurrences.setdefault(tool_call.call_id, []).append(
                    (message_index, call_index)
                )
            continue
        if message.role != "tool" or message.tool_call_id is None:
            continue
        pending = pending_call_occurrences.get(message.tool_call_id)
        if not pending:
            continue
        # Providers may reuse an id in a later turn, so bind each result to the
        # nearest preceding unmatched occurrence instead of treating ids globally.
        call_occurrence = pending.pop()
        result_index_by_call_occurrence[call_occurrence] = message_index
        matched_result_indices.add(message_index)

    repaired: list[Message] = []
    repairs: list[Message] = []

    for index, message in enumerate(source):
        if index in matched_result_indices:
            continue
        repaired.append(message)
        if message.role != "assistant" or not message.tool_calls:
            continue

        for call_index, tool_call in enumerate(message.tool_calls):
            result_index = result_index_by_call_occurrence.get((index, call_index))
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
