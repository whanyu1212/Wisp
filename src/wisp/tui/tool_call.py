"""Compact, literal argument formatting for Textual tool-card headers.

Built-in tools have small, stable schemas, so their most useful arguments can be
rendered as commands a person can scan (``grep /pattern/ in src``) instead of a
flat ``key=value`` dump. Extension tools remain open-ended and use the bounded
generic fallback.

Every value is appended to :class:`textual.content.Content` as literal text. No
tool-controlled string is parsed as markup, and large payload fields belonging
to write/edit calls are deliberately never included in the header.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Literal

from textual.content import Content

_VALUE_LIMIT = 64
_PATH_LIMIT = 80
_GENERIC_LIMIT = 160
_GENERIC_MAX_ITEMS = 8
_BUILTIN_LIMIT = 200
_NUMBER_LIMIT = 24
_TOOL_MUTED_STYLE = "$text-muted"

type ToolActionStatus = Literal["pending", "done", "error", "denied", "cancelled"]

_ACTION_WORDS: dict[str, dict[ToolActionStatus, str]] = {
    "bash": {
        "pending": "Running",
        "done": "Ran",
        "error": "Failed to run",
        "denied": "Denied running",
        "cancelled": "Cancelled running",
    },
    "read": {
        "pending": "Reading",
        "done": "Read",
        "error": "Failed to read",
        "denied": "Denied reading",
        "cancelled": "Cancelled reading",
    },
    "grep": {
        "pending": "Searching",
        "done": "Searched",
        "error": "Failed to search",
        "denied": "Denied searching",
        "cancelled": "Cancelled searching",
    },
    "find": {
        "pending": "Searching",
        "done": "Searched",
        "error": "Failed to search",
        "denied": "Denied searching",
        "cancelled": "Cancelled searching",
    },
    "ls": {
        "pending": "Listing",
        "done": "Listed",
        "error": "Failed to list",
        "denied": "Denied listing",
        "cancelled": "Cancelled listing",
    },
    "edit": {
        "pending": "Editing",
        "done": "Edited",
        "error": "Failed to edit",
        "denied": "Denied editing",
        "cancelled": "Cancelled editing",
    },
    "write": {
        "pending": "Writing",
        "done": "Wrote",
        "error": "Failed to write",
        "denied": "Denied writing",
        "cancelled": "Cancelled writing",
    },
}

_EXTENSION_ACTION_WORDS: dict[ToolActionStatus, str] = {
    "pending": "Calling",
    "done": "Called",
    "error": "Failed to call",
    "denied": "Denied calling",
    "cancelled": "Cancelled calling",
}


def format_tool_call_action(
    name: str,
    arguments: object,
    *,
    status: ToolActionStatus,
    arguments_available: bool = True,
) -> Content:
    """Return one literal, status-aware action label for a tool-card header."""

    words = _ACTION_WORDS.get(name)
    content = Content.styled((words or _EXTENSION_ACTION_WORDS)[status], "b")
    if words is None:
        content += Content(" ") + Content(name)
    if not arguments_available:
        return content + Content.styled("  (arguments unavailable)", _TOOL_MUTED_STYLE)
    rendered_arguments = format_tool_call_arguments(name, arguments)
    if rendered_arguments.plain:
        # Textual's terminal line wrapper consumes one break-space at a style
        # boundary; two literal cells preserve one visible separator between the
        # bold action and muted arguments without using a non-breaking character.
        content += Content("  ") + rendered_arguments
    return content


def format_tool_call_arguments(name: str, arguments: object) -> Content:
    """Return a compact literal argument description for one tool call."""

    if not isinstance(arguments, Mapping):
        text = _clip(_one_line(_safe_text(arguments)), _VALUE_LIMIT)
        return Content.styled(text, _TOOL_MUTED_STYLE)
    formatter = _FORMATTERS.get(name)
    if formatter is None:
        return _format_generic(arguments)
    rendered = formatter(arguments)
    if len(rendered.plain) <= _BUILTIN_LIMIT:
        return rendered
    return Content.styled(_clip(rendered.plain, _BUILTIN_LIMIT), _TOOL_MUTED_STYLE)


def _format_read(arguments: Mapping[object, object]) -> Content:
    content = _path_content(arguments.get("path"))
    offset = _positive_int(arguments.get("offset"))
    limit = _positive_int(arguments.get("limit"))
    if offset is not None or limit is not None:
        start = offset or 1
        end = start + limit - 1 if limit is not None else ""
        content += Content.styled(f":{start}-{end}", _TOOL_MUTED_STYLE)
    return content


def _format_grep(arguments: Mapping[object, object]) -> Content:
    pattern = _value(arguments.get("pattern"), default="")
    content = (
        Content.styled("/", _TOOL_MUTED_STYLE)
        + Content(pattern)
        + Content.styled("/ in ", _TOOL_MUTED_STYLE)
    )
    content += _path_content(arguments.get("path"), default=".")
    glob = _optional_value(arguments.get("glob"))
    if glob:
        content += (
            Content.styled(" (", _TOOL_MUTED_STYLE)
            + Content(glob)
            + Content.styled(")", _TOOL_MUTED_STYLE)
        )
    if arguments.get("ignore_case") is True:
        content += Content.styled(" · ignore case", _TOOL_MUTED_STYLE)
    if arguments.get("literal") is True:
        content += Content.styled(" · literal", _TOOL_MUTED_STYLE)
    context = _nonnegative_int(arguments.get("context"))
    if context:
        content += Content.styled(f" · context {context}", _TOOL_MUTED_STYLE)
    max_results = _positive_int(arguments.get("max_results"))
    if max_results is not None:
        content += Content.styled(f" · limit {max_results}", _TOOL_MUTED_STYLE)
    return content


def _format_find(arguments: Mapping[object, object]) -> Content:
    pattern = _value(arguments.get("pattern"), default="*")
    content = Content(pattern) + Content.styled(" in ", _TOOL_MUTED_STYLE)
    content += _path_content(arguments.get("path"), default=".")
    max_results = _positive_int(arguments.get("max_results"))
    if max_results is not None:
        content += Content.styled(f" · limit {max_results}", _TOOL_MUTED_STYLE)
    return content


def _format_ls(arguments: Mapping[object, object]) -> Content:
    content = _path_content(arguments.get("path"), default=".")
    if arguments.get("all") is True:
        content += Content.styled(" · hidden", _TOOL_MUTED_STYLE)
    return content


def _format_bash(arguments: Mapping[object, object]) -> Content:
    raw_operation = arguments.get("operation")
    operation = raw_operation if isinstance(raw_operation, str) else "run"
    if operation in {"poll", "cancel"}:
        content = Content.styled(f"{operation} ", _TOOL_MUTED_STYLE)
        content += Content(_value(arguments.get("process_id"), default="<process>"))
        if operation == "poll":
            wait = _nonnegative_number_text(arguments.get("wait_seconds"))
            if wait is not None:
                content += Content.styled(f" · wait {wait}s", _TOOL_MUTED_STYLE)
        return content

    content = Content("")
    if operation == "start":
        content += Content.styled("start ", _TOOL_MUTED_STYLE)
    content += Content(_value(arguments.get("command"), default=""))
    if operation == "start":
        lifetime = _positive_number_text(arguments.get("lifetime_seconds"))
        if lifetime is not None:
            content += Content.styled(f" · lifetime {lifetime}s", _TOOL_MUTED_STYLE)
        yield_seconds = _nonnegative_number_text(arguments.get("yield_seconds"))
        if yield_seconds is not None:
            content += Content.styled(f" · yield {yield_seconds}s", _TOOL_MUTED_STYLE)
    else:
        timeout = _positive_int(arguments.get("timeout"))
        if timeout is not None:
            content += Content.styled(f" · timeout {timeout}s", _TOOL_MUTED_STYLE)
    return content


def _format_edit(arguments: Mapping[object, object]) -> Content:
    content = _path_content(arguments.get("path"))
    edits = arguments.get("edits")
    if isinstance(edits, Sequence) and not isinstance(edits, str | bytes):
        count = len(edits)
        noun = "edit" if count == 1 else "edits"
        content += Content.styled(f" · {count} {noun}", _TOOL_MUTED_STYLE)
    return content


def _format_write(arguments: Mapping[object, object]) -> Content:
    return _path_content(arguments.get("path"))


def _format_generic(arguments: Mapping[object, object]) -> Content:
    parts: list[str] = []
    for index, (key, value) in enumerate(arguments.items()):
        if index >= _GENERIC_MAX_ITEMS:
            parts.append("…")
            break
        key_text = _clip(_one_line(_safe_text(key)), _VALUE_LIMIT)
        value_text = _clip(_one_line(_safe_text(value)), _VALUE_LIMIT)
        parts.append(f"{key_text}={value_text}")
    return Content.styled(_clip(", ".join(parts), _GENERIC_LIMIT), _TOOL_MUTED_STYLE)


def _path_content(value: object, *, default: str = "") -> Content:
    text = _safe_text(value) if isinstance(value, str) else default
    return Content(_clip_middle(_one_line(text), _PATH_LIMIT))


def _value(value: object, *, default: str) -> str:
    text = _safe_text(value) if isinstance(value, str) else default
    return _clip(_one_line(text), _VALUE_LIMIT)


def _optional_value(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return _clip(_one_line(value), _VALUE_LIMIT)


def _one_line(text: str) -> str:
    return (
        text.replace("\r\n", " ↵ ").replace("\r", " ↵ ").replace("\n", " ↵ ").replace("\t", " ⇥ ")
    )


def _safe_text(value: object) -> str:
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - extension-owned values must degrade safely
        return "<invalid>"


def _positive_int(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _nonnegative_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _positive_number_text(value: object) -> str | None:
    return _number_text(value, allow_zero=False)


def _nonnegative_number_text(value: object) -> str | None:
    return _number_text(value, allow_zero=True)


def _number_text(value: object, *, allow_zero: bool) -> str | None:
    if type(value) is int:
        if value > 0 or (allow_zero and value == 0):
            return _clip(str(value), _NUMBER_LIMIT)
        return None
    if type(value) is float and math.isfinite(value):
        if value > 0 or (allow_zero and value == 0):
            return _clip(f"{value:g}", _NUMBER_LIMIT)
    return None


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


def _clip_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = limit - 1
    head = keep // 2
    tail = keep - head
    return f"{text[:head]}…{text[-tail:]}"


_ToolFormatter = Callable[[Mapping[object, object]], Content]
_FORMATTERS: dict[str, _ToolFormatter] = {
    "read": _format_read,
    "grep": _format_grep,
    "find": _format_find,
    "ls": _format_ls,
    "bash": _format_bash,
    "edit": _format_edit,
    "write": _format_write,
}


__all__ = ["ToolActionStatus", "format_tool_call_action", "format_tool_call_arguments"]
