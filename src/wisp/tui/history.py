"""Renderer-neutral transcript hydration helpers for TUI resume flows."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from wisp.agent.transcript import INTERRUPTED_TOOL_RESULT_TEXT
from wisp.events import (
    JsonObject,
    RpcMessageSnapshot,
    RpcMessageToolCallSnapshot,
    ToolPresentationStatus,
)
from wisp.tui.skills import format_skill_invocation

TUI_HISTORY_MESSAGE_LIMIT = 500
TUI_HISTORY_PAGE_LIMIT = 75
_TRUNCATED_SUFFIX = "[content truncated]"


class HistoryHydrationPolicy(StrEnum):
    """How much durable history a renderer requests before its first render."""

    LATEST_PAGE = "latest_page"
    COMPLETE = "complete"


@dataclass(frozen=True)
class HistoricalTranscriptMessage:
    """One historical user/assistant line ready for TUI rendering."""

    role: Literal["system", "user", "assistant"]
    content: str
    entry_id: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class HistoricalToolCard:
    """One historical tool call/result ready for renderer-specific card mounting."""

    card_id: str
    name: str
    arguments: JsonObject
    output: str
    is_error: bool
    status: ToolPresentationStatus | None = None
    exit_code: int | None = None
    output_has_exit_status: bool = False
    before_text: str | None = None
    created: bool = False
    summary: str | None = None
    truncated: bool = False
    missing_result: bool = False
    tool_call_id: str | None = field(default=None, compare=False)
    call_missing: bool = field(default=False, compare=False)
    entry_id: str | None = field(default=None, compare=False)
    call_entry_id: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class HistoricalSkillInvocation:
    """One persisted explicit invocation ready for compact rendering."""

    entry_id: str
    name: str
    original_content: str
    original_content_truncated: bool
    request: str
    request_truncated: bool
    instructions_truncated: bool


type HistoricalTranscriptEntry = (
    HistoricalTranscriptMessage | HistoricalSkillInvocation | HistoricalToolCard
)


def represented_history_entry_ids(
    entries: tuple[HistoricalTranscriptEntry, ...],
) -> frozenset[str]:
    """Return persisted message IDs accounted for by logical transcript entries."""

    represented: set[str] = set()
    for entry in entries:
        entry_id = entry.entry_id
        if entry_id is not None:
            represented.add(entry_id)
        if isinstance(entry, HistoricalToolCard) and entry.call_entry_id is not None:
            represented.add(entry.call_entry_id)
    return frozenset(represented)


def history_from_rpc_messages(
    messages: tuple[RpcMessageSnapshot, ...],
) -> tuple[HistoricalTranscriptMessage, ...]:
    """Convert bounded RPC transcript messages into text-only TUI-visible history."""

    rendered: list[HistoricalTranscriptMessage] = []
    for entry in history_entries_from_rpc_messages(messages):
        if isinstance(entry, HistoricalTranscriptMessage):
            rendered.append(entry)
        elif isinstance(entry, HistoricalSkillInvocation):
            rendered.append(
                HistoricalTranscriptMessage(
                    role="user",
                    content=format_skill_invocation(
                        entry.name,
                        entry.request,
                        request_truncated=entry.request_truncated,
                        instructions_truncated=entry.instructions_truncated,
                    ),
                    entry_id=entry.entry_id,
                )
            )
    return tuple(rendered)


def history_entries_from_rpc_messages(
    messages: tuple[RpcMessageSnapshot, ...],
) -> tuple[HistoricalTranscriptEntry, ...]:
    """Convert bounded RPC transcript messages into ordered TUI history entries.

    Older message pages can begin or end in the middle of a tool-call exchange.
    Boundary-only call entries retain their call ID so a renderer can enrich the
    already-mounted result card without duplicating it.
    """

    rendered: list[HistoricalTranscriptEntry] = []
    pending_tool_calls: dict[str, tuple[RpcMessageToolCallSnapshot, str]] = {}
    for message in messages:
        if message.role == "system":
            # Provider-facing instructions remain durable and available through
            # RPC, but are not conversation turns and should not consume TUI
            # transcript space when a session is hydrated or resumed.
            continue
        elif message.role == "user":
            invocation = message.skill_invocation
            if invocation is not None:
                rendered.append(
                    HistoricalSkillInvocation(
                        entry_id=message.entry_id,
                        name=invocation.name,
                        original_content=invocation.original_content,
                        original_content_truncated=invocation.original_content_truncated,
                        request=invocation.request,
                        request_truncated=invocation.request_truncated,
                        instructions_truncated=invocation.instructions_truncated,
                    )
                )
            else:
                rendered.append(
                    HistoricalTranscriptMessage(
                        role="user",
                        content=_content_for_history(message),
                        entry_id=message.entry_id,
                    )
                )
        elif message.role == "assistant":
            if message.content or message.content_truncated:
                rendered.append(
                    HistoricalTranscriptMessage(
                        role="assistant",
                        content=_content_for_history(message),
                        entry_id=message.entry_id,
                    )
                )
            elif not message.tool_calls:
                rendered.append(
                    HistoricalTranscriptMessage(
                        role="assistant",
                        content="(empty assistant message)",
                        entry_id=message.entry_id,
                    )
                )
            pending_tool_calls.update(
                (tool_call.call_id, (tool_call, message.entry_id))
                for tool_call in message.tool_calls
            )
        elif message.role == "tool":
            rendered.append(_historical_tool_card(message, pending_tool_calls))
    rendered.extend(_missing_tool_cards(pending_tool_calls))
    return tuple(rendered)


def _content_for_history(message: RpcMessageSnapshot) -> str:
    content = message.content
    if not message.content_truncated:
        return content
    separator = "" if not content or content.endswith("\n") else "\n"
    return f"{content}{separator}{_TRUNCATED_SUFFIX}"


def _historical_tool_card(
    message: RpcMessageSnapshot,
    pending_tool_calls: dict[str, tuple[RpcMessageToolCallSnapshot, str]],
) -> HistoricalToolCard:
    pending_call = (
        pending_tool_calls.pop(message.tool_call_id, None)
        if message.tool_call_id is not None
        else None
    )
    tool_call = pending_call[0] if pending_call is not None else None
    tool_result = message.tool_result
    output = _content_for_history(message)
    status = tool_result.status if tool_result is not None else None
    if status is None and message.is_error and message.content == INTERRUPTED_TOOL_RESULT_TEXT:
        status = "cancelled"
    return HistoricalToolCard(
        card_id=f"history:{message.entry_id}",
        entry_id=message.entry_id,
        call_entry_id=pending_call[1] if pending_call is not None else None,
        name=message.tool_name or (tool_call.name if tool_call is not None else "unknown"),
        arguments=tool_call.arguments if tool_call is not None else {},
        output=output,
        is_error=bool(message.is_error),
        tool_call_id=message.tool_call_id,
        # A result can lack call metadata even when legacy persistence omitted the
        # call ID. Pairability still depends on tool_call_id elsewhere, but display
        # availability does not: never fabricate built-in defaults from `{}`.
        call_missing=tool_call is None,
        status=status,
        exit_code=tool_result.exit_code if tool_result is not None else None,
        output_has_exit_status=(
            tool_result.output_has_exit_status if tool_result is not None else False
        ),
        before_text=tool_result.before_text if tool_result is not None else None,
        created=tool_result.created if tool_result is not None else False,
        summary=tool_result.summary if tool_result is not None else None,
        truncated=(tool_result.truncated if tool_result is not None else False)
        or message.content_truncated,
    )


def _missing_tool_cards(
    pending_tool_calls: dict[str, tuple[RpcMessageToolCallSnapshot, str]],
) -> tuple[HistoricalToolCard, ...]:
    return tuple(
        HistoricalToolCard(
            card_id=f"history:missing:{call_id}",
            entry_id=entry_id,
            name=tool_call.name,
            arguments=tool_call.arguments,
            output="No persisted tool result.",
            is_error=True,
            tool_call_id=call_id,
            status="cancelled",
            missing_result=True,
        )
        for call_id, (tool_call, entry_id) in pending_tool_calls.items()
    )


def historical_tool_status(entry: HistoricalToolCard) -> ToolPresentationStatus:
    if entry.status is not None:
        return entry.status
    if entry.missing_result:
        return "cancelled"
    if entry.is_error or entry.exit_code not in {None, 0}:
        return "error"
    return "done"
