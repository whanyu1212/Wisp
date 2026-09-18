"""Bounded read models projected from persisted session entries.

Message pages and tree pages are what frontends and the RPC host consume
instead of full transcripts. Every projection here clips content to fixed
byte budgets so a large session cannot produce an unbounded response.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from wisp.agent.messages import Role
from wisp.events import (
    JsonObject,
    RpcMessageSnapshot,
    RpcMessageToolCallSnapshot,
    RpcMessageToolResultSnapshot,
    RpcSkillInvocationSnapshot,
    ToolCallSnapshot,
)
from wisp.sessions.entries import (
    CompactionSessionEntry,
    EventSessionEntry,
    MessageSessionEntry,
    SessionEntry,
    SessionTreeEntry,
    ToolResultPresentationSnapshot,
)
from wisp.sessions.errors import SessionError
from wisp.sessions.replay import resolve_session_tree
from wisp.skills.models import SkillInvocationEvidence

DEFAULT_SESSION_MESSAGE_PAGE_LIMIT = 200
MAX_SESSION_MESSAGE_PAGE_LIMIT = 500
DEFAULT_SESSION_TREE_PAGE_LIMIT = 200
MAX_SESSION_TREE_PAGE_LIMIT = 500
SESSION_TREE_PREVIEW_BYTE_LIMIT = 512
MESSAGE_CONTENT_BYTE_LIMIT = 64 * 1024
TOOL_ARGUMENTS_BYTE_LIMIT = 64 * 1024
MESSAGE_TOOL_CALL_LIMIT = 16
MESSAGE_PAGE_TEXT_BYTE_LIMIT = 512 * 1024


@dataclass(frozen=True, slots=True)
class SessionMessagePage:
    """Bounded active transcript page for frontend and RPC consumers."""

    session_id: str | None
    path: Path | None
    active_leaf_id: str | None
    messages: tuple[RpcMessageSnapshot, ...]
    truncated: bool
    next_before_entry_id: str | None
    next_after_entry_id: str | None = None


@dataclass(frozen=True, slots=True)
class MessagePageIndex:
    """Resolved active-path messages and their stable cursor positions."""

    active_leaf_id: str | None
    messages: tuple[MessageSessionEntry, ...]
    positions: dict[str, int]


@dataclass(frozen=True, slots=True)
class SessionTreeNodeSummary:
    """Bounded frontend-facing metadata for one persisted session tree node."""

    entry_id: str
    parent_id: str | None
    operation_id: str | None
    created_at: datetime
    kind: Literal["message", "event", "compaction"]
    role: Role | None
    preview: str
    preview_truncated: bool


@dataclass(frozen=True, slots=True)
class SessionTreePage:
    """One append-ordered page of a selected session tree."""

    session_id: str | None
    path: Path | None
    active_leaf_id: str | None
    total_node_count: int
    nodes: tuple[SessionTreeNodeSummary, ...]
    truncated: bool
    next_after_entry_id: str | None


@dataclass(slots=True)
class _MessagePageTextBudget:
    remaining: int


def validate_message_page_limit(limit: int) -> None:
    if limit < 1:
        raise ValueError("message page limit must be at least 1")
    if limit > MAX_SESSION_MESSAGE_PAGE_LIMIT:
        raise ValueError(f"message page limit cannot exceed {MAX_SESSION_MESSAGE_PAGE_LIMIT}")


def message_page_index_from_entries(
    entries: Iterable[SessionEntry],
) -> MessagePageIndex:
    tree = resolve_session_tree(tuple(entries))
    messages = tuple(entry for entry in tree.active_path if isinstance(entry, MessageSessionEntry))
    return MessagePageIndex(
        active_leaf_id=tree.active_leaf_id,
        messages=messages,
        positions={entry.id: index for index, entry in enumerate(messages)},
    )


def message_page_from_index(
    index: MessagePageIndex,
    *,
    session_id: str,
    path: Path,
    limit: int,
    before_entry_id: str | None,
    after_entry_id: str | None,
    entry_ids: tuple[str, ...] = (),
    complete_structure: bool = False,
    full_content: bool = False,
) -> SessionMessagePage:
    validate_message_page_limit(limit)
    if before_entry_id is not None and after_entry_id is not None:
        raise ValueError("message page cursors are mutually exclusive")
    if entry_ids and (before_entry_id is not None or after_entry_id is not None):
        raise ValueError("exact message entry IDs cannot be combined with page cursors")
    if full_content and not entry_ids:
        raise ValueError("full message content requires exact entry IDs")
    active_messages = index.messages
    if entry_ids:
        if len(entry_ids) > 16:
            raise ValueError("exact message entry lookup cannot exceed 16 entries")
        if len(set(entry_ids)) != len(entry_ids):
            raise ValueError("exact message entry IDs must be unique")
        if full_content and len(entry_ids) != 1:
            raise ValueError("full message content requires exactly one entry ID")
        missing = tuple(entry_id for entry_id in entry_ids if entry_id not in index.positions)
        if missing:
            raise SessionError(f"Session message entry not found on active path: {missing[0]}")
        requested = frozenset(entry_ids)
        selected = tuple(entry for entry in active_messages if entry.id in requested)
        truncated = False
    elif after_entry_id is not None:
        cursor_index = index.positions.get(after_entry_id)
        if cursor_index is None:
            raise SessionError(f"Session message cursor not found: {after_entry_id}")
        candidates = active_messages[cursor_index + 1 :]
        truncated = len(candidates) > limit
        selected = candidates[:limit]
    else:
        if before_entry_id is None:
            candidates = active_messages
        else:
            cursor_index = index.positions.get(before_entry_id)
            if cursor_index is None:
                raise SessionError(f"Session message cursor not found: {before_entry_id}")
            candidates = active_messages[:cursor_index]
        truncated = len(candidates) > limit
        selected = candidates[-limit:]
    text_budget = (
        None
        if full_content or complete_structure
        else _MessagePageTextBudget(remaining=MESSAGE_PAGE_TEXT_BYTE_LIMIT)
    )
    newest_first_messages = tuple(
        _rpc_message_snapshot(
            entry,
            text_budget=text_budget,
            complete_structure=complete_structure or full_content,
        )
        for entry in reversed(selected)
    )
    return SessionMessagePage(
        session_id=session_id,
        path=path,
        active_leaf_id=index.active_leaf_id,
        messages=tuple(reversed(newest_first_messages)),
        truncated=truncated,
        next_before_entry_id=(
            selected[0].id
            if not entry_ids and after_entry_id is None and truncated and selected
            else None
        ),
        next_after_entry_id=(
            selected[-1].id
            if not entry_ids and after_entry_id is not None and truncated and selected
            else None
        ),
    )


def _rpc_message_snapshot(
    entry: MessageSessionEntry,
    *,
    text_budget: _MessagePageTextBudget | None,
    complete_structure: bool = False,
) -> RpcMessageSnapshot:
    message = entry.message
    skill_invocation = _rpc_skill_invocation_snapshot(
        message.skill_invocation,
        text_budget=text_budget,
    )
    if text_budget is None:
        content = message.content
        content_original_bytes = len(content.encode("utf-8"))
        content_truncated = False
    else:
        content, content_original_bytes, content_truncated = _clip_text_with_budget(
            message.content,
            limit=MESSAGE_CONTENT_BYTE_LIMIT,
            text_budget=text_budget,
        )
    tool_calls = message.tool_calls or ()
    selected_tool_calls = tool_calls if complete_structure else tool_calls[:MESSAGE_TOOL_CALL_LIMIT]
    tool_result, tool_result_projection_truncated = _rpc_tool_result_snapshot(
        entry.tool_result,
        text_budget=text_budget,
    )
    return RpcMessageSnapshot(
        entry_id=entry.id,
        parent_id=entry.parent_id,
        operation_id=entry.operation_id,
        created_at=entry.created_at,
        role=message.role,
        content=content,
        content_original_bytes=content_original_bytes,
        content_truncated=content_truncated or tool_result_projection_truncated,
        tool_call_id=message.tool_call_id,
        tool_name=message.tool_name,
        tool_calls=tuple(
            _rpc_tool_call_snapshot(
                tool_call,
                text_budget=text_budget,
                preserve_process_identity=complete_structure,
            )
            for tool_call in selected_tool_calls
        ),
        tool_calls_original_count=len(tool_calls),
        tool_calls_truncated=not complete_structure and len(tool_calls) > MESSAGE_TOOL_CALL_LIMIT,
        response_id=message.response_id,
        finish_reason=message.finish_reason,
        is_error=message.is_error,
        usage=message.usage,
        cost=message.cost,
        tool_result=tool_result,
        skill_invocation=skill_invocation,
    )


def _rpc_skill_invocation_snapshot(
    invocation: SkillInvocationEvidence | None,
    *,
    text_budget: _MessagePageTextBudget | None,
) -> RpcSkillInvocationSnapshot | None:
    if invocation is None:
        return None
    if text_budget is None:
        request = invocation.request
        request_bytes = len(request.encode("utf-8"))
        request_truncated = False
        original = invocation.original_content
        original_bytes = len(original.encode("utf-8"))
        original_truncated = False
    else:
        request, request_bytes, request_truncated = _clip_text_with_budget(
            invocation.request,
            limit=MESSAGE_CONTENT_BYTE_LIMIT,
            text_budget=text_budget,
        )
        original, original_bytes, original_truncated = _clip_text_with_budget(
            invocation.original_content,
            limit=MESSAGE_CONTENT_BYTE_LIMIT,
            text_budget=text_budget,
        )
    return RpcSkillInvocationSnapshot(
        name=invocation.name,
        original_content=original,
        original_content_bytes=original_bytes,
        original_content_truncated=original_truncated,
        request=request,
        request_bytes=request_bytes,
        request_truncated=request_truncated,
        content_sha256=invocation.content_sha256,
        instructions_truncated=invocation.instructions_truncated,
    )


def _rpc_tool_result_snapshot(
    tool_result: ToolResultPresentationSnapshot | None,
    *,
    text_budget: _MessagePageTextBudget | None,
) -> tuple[RpcMessageToolResultSnapshot | None, bool]:
    if tool_result is None:
        return None, False
    before_text = tool_result.before_text
    projection_truncated = False
    truncated = tool_result.truncated
    if before_text is not None and text_budget is not None:
        clipped_before_text, _, before_text_truncated = _clip_text_with_budget(
            before_text,
            limit=MESSAGE_CONTENT_BYTE_LIMIT,
            text_budget=text_budget,
        )
        before_text = None if before_text_truncated else clipped_before_text
        projection_truncated = projection_truncated or before_text_truncated
        truncated = truncated or before_text_truncated
    summary = tool_result.summary
    if summary is not None and text_budget is not None:
        summary, _, summary_truncated = _clip_text_with_budget(
            summary,
            limit=MESSAGE_CONTENT_BYTE_LIMIT,
            text_budget=text_budget,
        )
        projection_truncated = projection_truncated or summary_truncated
        truncated = truncated or summary_truncated
    return (
        RpcMessageToolResultSnapshot(
            status=tool_result.status,
            exit_code=tool_result.exit_code,
            output_has_exit_status=tool_result.output_has_exit_status,
            before_text=before_text,
            created=tool_result.created,
            summary=summary,
            truncated=truncated,
        ),
        projection_truncated,
    )


def _rpc_tool_call_snapshot(
    tool_call: ToolCallSnapshot,
    *,
    text_budget: _MessagePageTextBudget | None,
    preserve_process_identity: bool = False,
) -> RpcMessageToolCallSnapshot:
    process_identity = (
        _process_tool_identity_arguments(tool_call) if preserve_process_identity else None
    )
    if text_budget is None:
        clipped_arguments = tool_call.arguments
        original_bytes = len(
            json.dumps(tool_call.arguments, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        truncated = False
    elif process_identity is not None:
        original_bytes = _json_object_byte_count(tool_call.arguments)
        preview_arguments = {
            key: value for key, value in tool_call.arguments.items() if key not in process_identity
        }
        if preview_arguments:
            clipped_preview, _preview_bytes, truncated = _clip_json_object(
                preview_arguments,
                limit=TOOL_ARGUMENTS_BYTE_LIMIT,
                text_budget=text_budget,
            )
        else:
            clipped_preview = {}
            truncated = False
        clipped_arguments = {**clipped_preview, **process_identity}
    else:
        clipped_arguments, original_bytes, truncated = _clip_json_object(
            tool_call.arguments,
            limit=TOOL_ARGUMENTS_BYTE_LIMIT,
            text_budget=text_budget,
        )
    return RpcMessageToolCallSnapshot(
        call_id=tool_call.call_id,
        name=tool_call.name,
        arguments=clipped_arguments,
        arguments_original_bytes=original_bytes,
        arguments_truncated=truncated,
        parse_error=tool_call.parse_error,
    )


def _process_tool_identity_arguments(tool_call: ToolCallSnapshot) -> JsonObject | None:
    """Return the structural Bash poll/cancel keys needed by transcript replay."""

    if tool_call.name != "bash":
        return None
    operation = tool_call.arguments.get("operation")
    process_id = tool_call.arguments.get("process_id")
    if not isinstance(operation, str) or operation not in {"poll", "cancel"}:
        return None
    if not isinstance(process_id, str) or not process_id.strip():
        return None
    return {"operation": operation, "process_id": process_id}


def _clip_text_with_budget(
    text: str,
    *,
    limit: int,
    text_budget: _MessagePageTextBudget,
) -> tuple[str, int, bool]:
    effective_limit = min(limit, max(text_budget.remaining, 0))
    clipped, original_bytes, truncated = _clip_text(text, limit=effective_limit)
    text_budget.remaining = max(text_budget.remaining - len(clipped.encode("utf-8")), 0)
    return clipped, original_bytes, truncated


def _clip_text(text: str, *, limit: int) -> tuple[str, int, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, len(encoded), False
    return encoded[:limit].decode("utf-8", errors="ignore"), len(encoded), True


def tree_page_from_entries(
    entries: Sequence[SessionEntry],
    *,
    session_id: str | None,
    path: Path | None,
    limit: int,
    after_entry_id: str | None,
) -> SessionTreePage:
    if type(limit) is not int or limit < 1 or limit > MAX_SESSION_TREE_PAGE_LIMIT:
        raise ValueError(
            f"Session tree page limit must be between 1 and {MAX_SESSION_TREE_PAGE_LIMIT}"
        )
    if after_entry_id is not None and not after_entry_id:
        raise ValueError("Session tree cursor must be non-empty")

    tree = resolve_session_tree(entries)
    start = 0
    if after_entry_id is not None:
        cursor_index = next(
            (index for index, node in enumerate(tree.nodes) if node.id == after_entry_id),
            None,
        )
        if cursor_index is None:
            raise SessionError(f"Session tree cursor not found: {after_entry_id}")
        start = cursor_index + 1

    selected = tree.nodes[start : start + limit]
    end = start + len(selected)
    truncated = end < len(tree.nodes)
    return SessionTreePage(
        session_id=session_id,
        path=path,
        active_leaf_id=tree.active_leaf_id,
        total_node_count=len(tree.nodes),
        nodes=tuple(session_tree_node_summary(node) for node in selected),
        truncated=truncated,
        next_after_entry_id=selected[-1].id if truncated and selected else None,
    )


def session_tree_node_summary(entry: SessionTreeEntry) -> SessionTreeNodeSummary:
    role: Role | None = None
    if isinstance(entry, MessageSessionEntry):
        role = entry.message.role
        preview = entry.message.content
        if not preview and entry.message.tool_calls:
            names = ", ".join(call.name for call in entry.message.tool_calls)
            preview = f"[tool calls: {names}]"
        kind: Literal["message", "event", "compaction"] = "message"
    elif isinstance(entry, EventSessionEntry):
        event_type = entry.event.payload.get("type")
        preview = event_type if isinstance(event_type, str) else "event"
        kind = "event"
    else:
        assert isinstance(entry, CompactionSessionEntry)
        preview = entry.compaction.summary
        kind = "compaction"

    clipped, _original_bytes, truncated = _clip_text(
        preview,
        limit=SESSION_TREE_PREVIEW_BYTE_LIMIT,
    )
    return SessionTreeNodeSummary(
        entry_id=entry.id,
        parent_id=entry.parent_id,
        operation_id=entry.operation_id,
        created_at=entry.created_at,
        kind=kind,
        role=role,
        preview=clipped,
        preview_truncated=truncated,
    )


def _clip_json_object(
    arguments: JsonObject,
    *,
    limit: int,
    text_budget: _MessagePageTextBudget,
) -> tuple[JsonObject, int, bool]:
    rendered = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    encoded = rendered.encode("utf-8")
    effective_limit = min(limit, max(text_budget.remaining, 0))
    if len(encoded) <= effective_limit:
        text_budget.remaining = max(text_budget.remaining - len(encoded), 0)
        return dict(arguments), len(encoded), False
    preview_arguments, preview_bytes = _clip_json_preview_wrapper(
        rendered,
        limit=effective_limit,
    )
    text_budget.remaining = max(text_budget.remaining - preview_bytes, 0)
    return preview_arguments, len(encoded), True


def _clip_json_preview_wrapper(rendered: str, *, limit: int) -> tuple[JsonObject, int]:
    empty_preview: JsonObject = {"truncated_json_preview": ""}
    empty_preview_bytes = _json_object_byte_count(empty_preview)
    if limit < empty_preview_bytes:
        return {}, 0

    rendered_bytes = rendered.encode("utf-8")
    low = 0
    high = len(rendered_bytes)
    best_preview = ""
    best_byte_count = empty_preview_bytes
    while low <= high:
        midpoint = (low + high) // 2
        preview = rendered_bytes[:midpoint].decode("utf-8", errors="ignore")
        candidate: JsonObject = {"truncated_json_preview": preview}
        candidate_byte_count = _json_object_byte_count(candidate)
        if candidate_byte_count <= limit:
            best_preview = preview
            best_byte_count = candidate_byte_count
            low = midpoint + 1
        else:
            high = midpoint - 1
    return {"truncated_json_preview": best_preview}, best_byte_count


def _json_object_byte_count(value: JsonObject) -> int:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return len(rendered.encode("utf-8"))
