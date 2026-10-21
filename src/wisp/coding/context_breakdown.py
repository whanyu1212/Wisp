"""Split a context budget's effective tokens by the source of the context."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from wisp.agent.context_budget import message_tokens, text_tokens, tool_schema_tokens
from wisp.coding.prompt import PromptPart
from wisp.events import (
    ContextBreakdown,
    ContextItem,
    ContextSource,
    ContextSourceItem,
    ContextSourceUsage,
    ContextTurnGrowth,
    ToolCallSnapshot,
)
from wisp.messages import Message
from wisp.providers.base import ToolSpec
from wisp.sessions.entries import MessageSessionEntry, SessionEntry
from wisp.sessions.replay import SessionReplay

# Rows every breakdown lists, even at zero, so frontends can keep a stable layout.
# `custom_prompt` is listed only when the session has a caller-supplied prompt.
_STANDARD_SOURCES: tuple[ContextSource, ...] = (
    "messages",
    "built_in_tools",
    "mcp_tools",
    "system_prompt",
    "skills",
    "project_instructions",
    "project_context",
)
_SOURCE_ORDER: tuple[ContextSource, ...] = (
    "messages",
    "built_in_tools",
    "mcp_tools",
    "custom_prompt",
    "system_prompt",
    "skills",
    "project_instructions",
    "project_context",
)
_MESSAGE_ROLE_LABELS = {"tool": "tool output", "assistant": "assistant", "user": "user"}
# What a source's `count` reports: the items its children hold ("items"), its
# number of children ("children"), or nothing. Only "items" sources also report
# a count per child, since only their children hold several items.
_SOURCE_COUNTS: dict[ContextSource, Literal["items", "children"]] = {
    "messages": "items",
    "mcp_tools": "items",
    "built_in_tools": "children",
    "project_instructions": "children",
}
_MCP_PREFIX = "mcp__"
_LARGEST_ITEM_COUNT = 3
_MAX_LABEL_CHARS = 60


@dataclass(slots=True)
class _Child:
    """Raw estimate for one part of a source, before apportioning."""

    tokens: int = 0
    count: int = 0


def build_context_breakdown(
    *,
    prompt_messages: Sequence[Message],
    prompt_parts: Sequence[Sequence[PromptPart]],
    tools: Sequence[ToolSpec],
    replay: SessionReplay,
    entries: Sequence[SessionEntry],
    effective_tokens: int,
    scaled: bool,
) -> ContextBreakdown:
    """Split `effective_tokens` across context sources, turns, and large messages.

    Each source is estimated with the same heuristic as the context budget, then
    every figure is apportioned (largest remainder) so the sources, each source's
    children, and the turns all sum exactly to `effective_tokens`. Provider
    overhead the heuristic cannot see is thereby spread across the rows.

    Args:
        prompt_messages (Sequence[Message]): The prompt's system messages, as sent.
        prompt_parts (Sequence[Sequence[PromptPart]]): Labelled parts of each prompt
            message, aligned by index with `prompt_messages`.
        tools (Sequence[ToolSpec]): Tool schemas sent with the request.
        replay (SessionReplay): Active context, whose messages are measured by role
            and grouped by run.
        entries (Sequence[SessionEntry]): Session entries the replay came from.
        effective_tokens (int): The budget total to apportion.
        scaled (bool): Whether that total is a provider observation.

    Returns:
        ContextBreakdown: Sources in display order, per-turn growth since the last
        compaction, and the largest single messages.
    """

    # Messages are measured as saved rather than as normalized for the provider:
    # providers without native tool history fold each tool result into an
    # assistant envelope, which would hide tool output. Apportioning absorbs the
    # small size difference.
    turn_numbers = _turn_numbers(replay, entries)
    history = [
        (turn_numbers.get(row.entry_id), row.message)
        for row in replay.rows
        if row.message.role != "system"
    ]
    raw = _raw_source_children(
        prompt_messages,
        prompt_parts,
        [message for _turn, message in history],
        tools,
    )
    sources = [source for source in _SOURCE_ORDER if source in _STANDARD_SOURCES or raw[source]]
    source_raw = [sum(child.tokens for child in raw[source].values()) for source in sources]
    source_tokens = _apportion(source_raw, effective_tokens)
    usages = tuple(
        _source_usage(source, raw[source], tokens)
        for source, tokens in zip(sources, source_tokens, strict=True)
    )

    # The prompt and tool schemas were all sent with the first reported turn.
    fixed_raw = sum(source_raw) - source_raw[sources.index("messages")]
    turns = _turn_growth(history, fixed_raw=fixed_raw, total=effective_tokens)
    scale = effective_tokens / sum(source_raw) if sum(source_raw) else 0.0
    return ContextBreakdown(
        sources=usages,
        scaled=scaled,
        turns=turns,
        largest=_largest_items(history, scale=scale),
    )


def _raw_source_children(
    prompt_messages: Sequence[Message],
    prompt_parts: Sequence[Sequence[PromptPart]],
    history: Sequence[Message],
    tools: Sequence[ToolSpec],
) -> dict[ContextSource, dict[str, _Child]]:
    raw: dict[ContextSource, dict[str, _Child]] = {source: {} for source in _SOURCE_ORDER}
    for message, parts in zip(prompt_messages, prompt_parts, strict=True):
        _add_prompt_message(raw, message, parts)
    for message in history:
        child = raw["messages"].setdefault(
            _MESSAGE_ROLE_LABELS.get(message.role, message.role), _Child()
        )
        child.tokens += message_tokens(message)
        child.count += 1
    for tool in tools:
        if tool.name.startswith(_MCP_PREFIX):
            server = tool.name[len(_MCP_PREFIX) :].split("__", 1)[0]
            child = raw["mcp_tools"].setdefault(server, _Child())
        else:
            child = raw["built_in_tools"].setdefault(tool.name, _Child())
        child.tokens += tool_schema_tokens(tool)
        child.count += 1
    return raw


def _add_prompt_message(
    raw: Mapping[ContextSource, dict[str, _Child]],
    message: Message,
    parts: Sequence[PromptPart],
) -> None:
    """Split one prompt message's measured tokens across its labelled parts."""

    weights = [text_tokens(part.text) for part in parts]
    for part, tokens in zip(parts, _apportion(weights, message_tokens(message)), strict=True):
        child = raw[part.source].setdefault(part.label, _Child())
        child.tokens += tokens
        child.count += 1


def _source_usage(
    source: ContextSource,
    children: Mapping[str, _Child],
    tokens: int,
) -> ContextSourceUsage:
    counts = _SOURCE_COUNTS.get(source)
    labels = list(children)
    child_tokens = _apportion([children[label].tokens for label in labels], tokens)
    items = tuple(
        ContextSourceItem(
            label=label,
            tokens=child_tokens[index],
            count=children[label].count if counts == "items" else None,
        )
        for index, label in enumerate(labels)
    )
    count: int | None
    if counts == "items":
        count = sum(child.count for child in children.values())
    elif counts == "children":
        count = len(children)
    else:
        count = None
    return ContextSourceUsage(source=source, tokens=tokens, count=count, children=items)


def _turn_numbers(replay: SessionReplay, entries: Sequence[SessionEntry]) -> dict[str, int]:
    """Number the runs along the active path, counting runs a compaction replaced.

    A run is the messages one operation appended. Messages saved without an
    operation start a new turn at each user message.
    """

    by_id = {entry.id: entry for entry in entries}
    numbers: dict[str, int] = {}
    turn = 0
    current_operation: str | None = None
    for entry_id in replay.path_entry_ids:
        entry = by_id.get(entry_id)
        if not isinstance(entry, MessageSessionEntry) or entry.message.role == "system":
            continue
        operation = entry.operation_id
        if turn == 0:
            starts_turn = True
        elif operation is not None:
            starts_turn = operation != current_operation
        else:
            starts_turn = entry.message.role == "user"
        if starts_turn:
            turn += 1
        current_operation = operation
        numbers[entry.id] = turn
    return numbers


def _turn_growth(
    history: Sequence[tuple[int | None, Message]],
    *,
    fixed_raw: int,
    total: int,
) -> tuple[ContextTurnGrowth, ...]:
    """Apportion the total across turns; the first turn also carries the fixed context."""

    raw: dict[int, int] = {}
    first_turn = next((turn for turn, _message in history if turn is not None), 1)
    for turn, message in history:
        key = turn if turn is not None else first_turn
        raw[key] = raw.get(key, 0) + message_tokens(message)
    raw[first_turn] = raw.get(first_turn, 0) + fixed_raw
    turns = sorted(raw)
    tokens = _apportion([raw[turn] for turn in turns], total)
    return tuple(
        ContextTurnGrowth(turn=turn, tokens=count)
        for turn, count in zip(turns, tokens, strict=True)
    )


def _largest_items(
    history: Sequence[tuple[int | None, Message]],
    *,
    scale: float,
) -> tuple[ContextItem, ...]:
    calls = {call.call_id: call for _turn, message in history for call in message.tool_calls or ()}
    sized = sorted(
        (
            (message_tokens(message), index)
            for index, (turn, message) in enumerate(history)
            if turn is not None
        ),
        reverse=True,
    )
    items = []
    for raw_tokens, index in sized[:_LARGEST_ITEM_COUNT]:
        turn, message = history[index]
        assert turn is not None
        items.append(
            ContextItem(
                label=_item_label(message, calls),
                tokens=round(raw_tokens * scale),
                turn=turn,
            )
        )
    return tuple(items)


def _item_label(message: Message, calls: Mapping[str, ToolCallSnapshot]) -> str:
    """Name a message by its tool and leading argument, or by its role."""

    if message.role != "tool":
        return "user prompt" if message.role == "user" else "assistant reply"
    name = message.tool_name or "tool"
    call = calls.get(message.tool_call_id or "")
    arguments = call.arguments if call is not None else {}
    argument = next((value for value in arguments.values() if isinstance(value, str)), "")
    return _clip(" ".join(f"{name} {argument}".split()) + " output")


def _clip(text: str) -> str:
    if len(text) <= _MAX_LABEL_CHARS:
        return text
    return f"{text[: _MAX_LABEL_CHARS - 1].rstrip()}…"


def _apportion(weights: Sequence[int], total: int) -> list[int]:
    """Split `total` in proportion to `weights` so the parts sum exactly to it.

    Uses largest-remainder rounding; ties go to the earlier weight. With no
    positive weight the total is split evenly.

    Examples:
        >>> _apportion([1, 1, 1], 10)
        [4, 3, 3]
        >>> _apportion([0, 5], 3)
        [0, 3]
    """

    if not weights:
        return []
    weight_sum = sum(weights)
    effective = list(weights) if weight_sum > 0 else [1] * len(weights)
    weight_sum = sum(effective)
    shares = [total * weight // weight_sum for weight in effective]
    remainders = sorted(
        range(len(effective)),
        key=lambda index: (-(total * effective[index] % weight_sum), index),
    )
    for index in remainders[: total - sum(shares)]:
        shares[index] += 1
    return shares


__all__ = ["build_context_breakdown"]
