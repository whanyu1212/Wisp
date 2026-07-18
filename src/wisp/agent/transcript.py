"""Provider-neutral transcript repair helpers."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from wisp.agent.messages import Message
from wisp.events import ToolCallSnapshot

INTERRUPTED_TOOL_RESULT_TEXT = (
    "Tool call interrupted before completion; execution outcome is unknown."
)


@dataclass(frozen=True, slots=True)
class TranscriptRepairPlan:
    """Logical transcript plus newly synthesized messages that require persistence."""

    messages: tuple[Message, ...]
    repairs: tuple[Message, ...]


@dataclass(frozen=True, slots=True)
class _MissingToolResult:
    tool_call: ToolCallSnapshot


def _order_tool_result_items[TranscriptItem](
    items: Sequence[TranscriptItem],
    *,
    message_of: Callable[[TranscriptItem], Message],
) -> tuple[TranscriptItem | _MissingToolResult, ...]:
    """Place each result after its nearest preceding unmatched call occurrence."""

    source = tuple(items)
    result_index_by_call_occurrence: dict[tuple[int, int], int] = {}
    pending_call_occurrences: dict[str, list[tuple[int, int]]] = {}
    matched_result_indices: set[int] = set()

    for item_index, item in enumerate(source):
        message = message_of(item)
        if message.role == "assistant" and message.tool_calls:
            for call_index, tool_call in enumerate(message.tool_calls):
                pending_call_occurrences.setdefault(tool_call.call_id, []).append(
                    (item_index, call_index)
                )
            continue
        if message.role != "tool" or message.tool_call_id is None:
            continue
        pending = pending_call_occurrences.get(message.tool_call_id)
        if not pending:
            continue
        call_occurrence = pending.pop()
        result_index_by_call_occurrence[call_occurrence] = item_index
        matched_result_indices.add(item_index)

    ordered: list[TranscriptItem | _MissingToolResult] = []
    for item_index, item in enumerate(source):
        if item_index in matched_result_indices:
            continue
        ordered.append(item)
        message = message_of(item)
        if message.role != "assistant" or not message.tool_calls:
            continue
        for call_index, tool_call in enumerate(message.tool_calls):
            result_index = result_index_by_call_occurrence.get((item_index, call_index))
            if result_index is None:
                ordered.append(_MissingToolResult(tool_call))
            else:
                ordered.append(source[result_index])
    return tuple(ordered)


def plan_interrupted_tool_repairs(messages: Sequence[Message]) -> TranscriptRepairPlan:
    """Pair tool results with calls and synthesize errors for missing results.

    Persisted sessions remain append-only, so an older repair may physically appear
    after later messages. The returned logical transcript moves each matched result
    next to its assistant call without rewriting the durable audit log.
    """

    repaired: list[Message] = []
    repairs: list[Message] = []

    for item in _order_tool_result_items(messages, message_of=lambda message: message):
        if isinstance(item, _MissingToolResult):
            tool_call = item.tool_call
            repair = Message(
                role="tool",
                content=INTERRUPTED_TOOL_RESULT_TEXT,
                tool_call_id=tool_call.call_id,
                tool_name=tool_call.name,
                is_error=True,
            )
            repaired.append(repair)
            repairs.append(repair)
        else:
            repaired.append(item)

    return TranscriptRepairPlan(messages=tuple(repaired), repairs=tuple(repairs))


__all__ = [
    "INTERRUPTED_TOOL_RESULT_TEXT",
    "TranscriptRepairPlan",
    "plan_interrupted_tool_repairs",
]
