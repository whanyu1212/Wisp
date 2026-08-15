"""Shared helpers for built-in tools."""

from __future__ import annotations

from collections.abc import Mapping

from wisp.tools.context import ToolContext
from wisp.tools.result import ToolArgumentError
from wisp.tools.truncation import TruncatedText, truncate_text


def _truncate_text(
    text: str,
    *,
    context: ToolContext,
    force_truncated: bool = False,
) -> TruncatedText:
    if force_truncated:
        separator = "" if not text or text.endswith("\n") else "\n"
        text = f"{text}{separator}[truncated]"
    return truncate_text(
        text,
        max_bytes=context.max_output_bytes,
        max_lines=context.max_output_lines,
    )


def _required_string(
    arguments: Mapping[str, object],
    name: str,
    *,
    allow_empty: bool = False,
    allow_whitespace: bool = False,
) -> str:
    value = arguments.get(name)
    if not isinstance(value, str):
        raise ToolArgumentError(f"{name} must be a string")
    if not allow_empty:
        is_empty = value == "" if allow_whitespace else not value.strip()
        if is_empty:
            raise ToolArgumentError(f"{name} must not be empty")
    return value


def _optional_string(arguments: Mapping[str, object], name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolArgumentError(f"{name} must be a string")
    return value


def _optional_int(
    arguments: Mapping[str, object],
    name: str,
    *,
    default: int | None = None,
) -> int | None:
    value = arguments.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolArgumentError(f"{name} must be an integer")
    return value


def _optional_bool(arguments: Mapping[str, object], name: str, *, default: bool) -> bool:
    value = arguments.get(name)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ToolArgumentError(f"{name} must be a boolean")
    return value
