"""Bounded semantic span discovery for the Textual prompt editor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from wisp.tui.file_index import parse_file_reference

MAX_PROMPT_HIGHLIGHT_LINE_CHARACTERS = 32_768
MAX_PROMPT_HIGHLIGHTS_PER_LINE = 256

PromptHighlightKind = Literal["command", "resolved_path", "unresolved_path"]


@dataclass(frozen=True, slots=True)
class PromptHighlight:
    """One codepoint-indexed semantic span within a prompt line."""

    start: int
    end: int
    kind: PromptHighlightKind


def prompt_line_highlights(
    line: str,
    *,
    line_index: int,
    line_count: int,
    command_tokens: frozenset[str],
    project_paths: frozenset[str] | None,
    unresolved_paths_known: bool,
) -> tuple[PromptHighlight, ...]:
    """Return bounded semantic spans without consulting mutable runtime state.

    ``project_paths is None`` means no project snapshot is available. When a
    snapshot is bounded or otherwise incomplete, known entries can still be
    resolved while ``unresolved_paths_known=False`` keeps absent paths neutral.
    """

    scan_limit = min(len(line), MAX_PROMPT_HIGHLIGHT_LINE_CHARACTERS)
    highlights: list[PromptHighlight] = []

    if line_index == 0:
        command = _command_highlight(
            line,
            scan_limit=scan_limit,
            line_count=line_count,
            command_tokens=command_tokens,
        )
        if command is not None:
            highlights.append(command)

    if project_paths is None:
        return tuple(highlights)

    cursor = 0
    while cursor < scan_limit and len(highlights) < MAX_PROMPT_HIGHLIGHTS_PER_LINE:
        at_index = line.find("@", cursor, scan_limit)
        if at_index == -1:
            break
        if at_index > 0 and not line[at_index - 1].isspace():
            cursor = at_index + 1
            continue
        if at_index + 1 >= scan_limit:
            break

        reference = parse_file_reference(
            line,
            start=at_index,
            limit=scan_limit,
        )
        if reference is None:
            cursor = at_index + 1
            continue
        end, path = reference
        if path is not None and path in project_paths:
            highlights.append(PromptHighlight(at_index, end, "resolved_path"))
        elif unresolved_paths_known:
            highlights.append(PromptHighlight(at_index, end, "unresolved_path"))
        cursor = max(end, at_index + 1)

    return tuple(highlights)


def _command_highlight(
    line: str,
    *,
    scan_limit: int,
    line_count: int,
    command_tokens: frozenset[str],
) -> PromptHighlight | None:
    start = 0
    while start < scan_limit and line[start].isspace():
        start += 1
    if start >= scan_limit or line[start] != "/":
        return None

    end = start + 1
    while end < scan_limit and not line[end].isspace():
        end += 1
    token = line[start:end]
    if line_count == 1 and token in command_tokens:
        return PromptHighlight(start, end, "command")
    return None


__all__ = [
    "MAX_PROMPT_HIGHLIGHT_LINE_CHARACTERS",
    "MAX_PROMPT_HIGHLIGHTS_PER_LINE",
    "PromptHighlight",
    "PromptHighlightKind",
    "prompt_line_highlights",
]
