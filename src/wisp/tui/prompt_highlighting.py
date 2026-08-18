"""Bounded semantic span discovery for the Textual prompt editor."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from wisp.tui.file_index import parse_file_reference

MAX_PROMPT_HIGHLIGHT_LINE_CHARACTERS = 32_768
MAX_PROMPT_HIGHLIGHT_DOCUMENT_CHARACTERS = 128 * 1024
MAX_PROMPT_HIGHLIGHT_LINES = 4_096
MAX_PROMPT_HIGHLIGHTS_PER_LINE = 256
MAX_PROMPT_HIGHLIGHTS_PER_DOCUMENT = 4_096
_MAX_PROMPT_INLINE_CODE_RUNS_PER_LINE = 2_048

PromptHighlightKind = Literal[
    "command",
    "resolved_path",
    "unresolved_path",
    "markdown_heading",
    "markdown_list_marker",
    "markdown_inline_code_delimiter",
    "markdown_inline_code",
    "markdown_fence_delimiter",
    "markdown_fence_info",
    "markdown_fence_body",
]


@dataclass(frozen=True, slots=True)
class PromptHighlight:
    """One codepoint-indexed semantic span within a prompt line."""

    start: int
    end: int
    kind: PromptHighlightKind


@dataclass(frozen=True, slots=True)
class _Fence:
    character: str
    length: int


def prompt_line_highlights(
    line: str,
    *,
    line_index: int,
    line_count: int,
    command_tokens: frozenset[str],
    project_paths: frozenset[str] | None,
    unresolved_paths_known: bool,
) -> tuple[PromptHighlight, ...]:
    """Return catalog-backed spans for one line without mutable runtime state.

    ``project_paths is None`` means no project snapshot is available. When a
    snapshot is bounded or otherwise incomplete, known entries can still be
    resolved while ``unresolved_paths_known=False`` keeps absent paths neutral.
    Markdown spans are discovered by :func:`prompt_document_highlights`, which
    can carry fenced-code state across lines.
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


def prompt_document_highlights(
    lines: Sequence[str],
    *,
    command_tokens: frozenset[str],
    project_paths: frozenset[str] | None,
    unresolved_paths_known: bool,
) -> tuple[tuple[PromptHighlight, ...], ...]:
    """Return bounded presentation spans for the scanned prefix of a prompt.

    The scanner intentionally recognizes only restrained Markdown structure. It
    does not render Markdown, load grammars, or mutate source text. Broad
    Markdown styles are returned before specific code styles and catalog-backed
    command/path styles so callers can apply them in deterministic precedence
    order.
    """

    line_count = len(lines)
    scanned: list[tuple[PromptHighlight, ...]] = []
    remaining_characters = MAX_PROMPT_HIGHLIGHT_DOCUMENT_CHARACTERS
    remaining_highlights = MAX_PROMPT_HIGHLIGHTS_PER_DOCUMENT
    fence: _Fence | None = None

    for line_index, line in enumerate(lines[:MAX_PROMPT_HIGHLIGHT_LINES]):
        if remaining_characters <= 0 or remaining_highlights <= 0:
            break
        scan_limit = min(
            len(line),
            MAX_PROMPT_HIGHLIGHT_LINE_CHARACTERS,
            remaining_characters,
        )
        complete_line = scan_limit == len(line)
        semantic = prompt_line_highlights(
            line,
            line_index=line_index,
            line_count=line_count,
            command_tokens=command_tokens,
            project_paths=project_paths,
            unresolved_paths_known=unresolved_paths_known,
        )
        semantic = tuple(highlight for highlight in semantic if highlight.end <= scan_limit)
        markdown, fence = _markdown_line_highlights(
            line,
            scan_limit=scan_limit,
            complete_line=complete_line,
            fence=fence,
        )

        line_limit = min(MAX_PROMPT_HIGHLIGHTS_PER_LINE, remaining_highlights)
        semantic = semantic[:line_limit]
        markdown_limit = max(0, line_limit - len(semantic))
        combined = (*markdown[:markdown_limit], *semantic)
        scanned.append(combined)
        remaining_highlights -= len(combined)
        remaining_characters -= scan_limit + (1 if line_index + 1 < line_count else 0)

    return tuple(scanned)


def _markdown_line_highlights(
    line: str,
    *,
    scan_limit: int,
    complete_line: bool,
    fence: _Fence | None,
) -> tuple[tuple[PromptHighlight, ...], _Fence | None]:
    if fence is not None:
        closing = _closing_fence(
            line, scan_limit=scan_limit, complete_line=complete_line, fence=fence
        )
        if closing is not None:
            return ((PromptHighlight(closing[0], closing[1], "markdown_fence_delimiter"),), None)
        if scan_limit:
            return ((PromptHighlight(0, scan_limit, "markdown_fence_body"),), fence)
        return (), fence

    opening = _opening_fence(line, scan_limit=scan_limit)
    if opening is not None:
        delimiter_start, delimiter_end, info_start, info_end, opened_fence = opening
        highlights = [PromptHighlight(delimiter_start, delimiter_end, "markdown_fence_delimiter")]
        if info_start < info_end:
            highlights.append(PromptHighlight(info_start, info_end, "markdown_fence_info"))
        return tuple(highlights), opened_fence

    broad: list[PromptHighlight] = []
    heading = _heading_highlight(line, scan_limit=scan_limit)
    if heading is not None:
        broad.append(heading)
    else:
        list_marker = _list_marker_highlight(line, scan_limit=scan_limit)
        if list_marker is not None:
            broad.append(list_marker)

    return (*broad, *_inline_code_highlights(line, scan_limit=scan_limit)), None


def _opening_fence(
    line: str,
    *,
    scan_limit: int,
) -> tuple[int, int, int, int, _Fence] | None:
    start = _after_optional_indent(line, scan_limit=scan_limit)
    if start >= scan_limit or line[start] not in {"`", "~"}:
        return None
    delimiter_end = _run_end(line, start=start, limit=scan_limit)
    delimiter_length = delimiter_end - start
    if delimiter_length < 3:
        return None
    remainder = line[delimiter_end:scan_limit]
    if line[start] == "`" and "`" in remainder:
        return None

    info_start = delimiter_end
    while info_start < scan_limit and line[info_start].isspace():
        info_start += 1
    info_end = info_start
    while info_end < scan_limit and not line[info_end].isspace():
        info_end += 1
    return (
        start,
        delimiter_end,
        info_start,
        info_end,
        _Fence(line[start], delimiter_length),
    )


def _closing_fence(
    line: str,
    *,
    scan_limit: int,
    complete_line: bool,
    fence: _Fence,
) -> tuple[int, int] | None:
    if not complete_line:
        return None
    start = _after_optional_indent(line, scan_limit=scan_limit)
    if start >= scan_limit or line[start] != fence.character:
        return None
    delimiter_end = _run_end(line, start=start, limit=scan_limit)
    if delimiter_end - start < fence.length:
        return None
    if line[delimiter_end:scan_limit].strip():
        return None
    return start, delimiter_end


def _heading_highlight(line: str, *, scan_limit: int) -> PromptHighlight | None:
    start = _after_optional_indent(line, scan_limit=scan_limit)
    if start >= scan_limit or line[start] != "#":
        return None
    marker_end = _run_end(line, start=start, limit=scan_limit)
    level = marker_end - start
    if level > 6:
        return None
    if marker_end < scan_limit and not line[marker_end].isspace():
        return None
    return PromptHighlight(start, scan_limit, "markdown_heading")


def _list_marker_highlight(line: str, *, scan_limit: int) -> PromptHighlight | None:
    start = _after_optional_indent(line, scan_limit=scan_limit)
    if start >= scan_limit:
        return None
    if line[start] in {"-", "*", "+"}:
        end = start + 1
        if end == scan_limit or line[end].isspace():
            return PromptHighlight(start, end, "markdown_list_marker")
        return None
    if not line[start].isdigit():
        return None

    end = start
    while end < scan_limit and line[end].isdigit() and end - start < 10:
        end += 1
    digit_count = end - start
    if not 1 <= digit_count <= 9 or end >= scan_limit or line[end] not in {".", ")"}:
        return None
    marker_end = end + 1
    if marker_end < scan_limit and not line[marker_end].isspace():
        return None
    return PromptHighlight(start, marker_end, "markdown_list_marker")


def _inline_code_highlights(line: str, *, scan_limit: int) -> tuple[PromptHighlight, ...]:
    runs: list[tuple[int, int]] = []
    cursor = 0
    while cursor < scan_limit:
        run_start = line.find("`", cursor, scan_limit)
        if run_start == -1:
            break
        run_end = _run_end(line, start=run_start, limit=scan_limit)
        runs.append((run_start, run_end))
        if len(runs) > _MAX_PROMPT_INLINE_CODE_RUNS_PER_LINE:
            return ()
        cursor = run_end

    next_same_length: list[int | None] = [None] * len(runs)
    next_run_by_length: dict[int, int] = {}
    for index in range(len(runs) - 1, -1, -1):
        run_start, run_end = runs[index]
        run_length = run_end - run_start
        next_same_length[index] = next_run_by_length.get(run_length)
        next_run_by_length[run_length] = index

    highlights: list[PromptHighlight] = []
    index = 0
    while index < len(runs) and len(highlights) < MAX_PROMPT_HIGHLIGHTS_PER_LINE:
        closing_index = next_same_length[index]
        if closing_index is None:
            index += 1
            continue
        opening_start, opening_end = runs[index]
        closing_start, closing_end = runs[closing_index]
        highlights.append(
            PromptHighlight(
                opening_start,
                opening_end,
                "markdown_inline_code_delimiter",
            )
        )
        if opening_end < closing_start:
            highlights.append(PromptHighlight(opening_end, closing_start, "markdown_inline_code"))
        highlights.append(
            PromptHighlight(
                closing_start,
                closing_end,
                "markdown_inline_code_delimiter",
            )
        )
        index = closing_index + 1
    return tuple(highlights[:MAX_PROMPT_HIGHLIGHTS_PER_LINE])


def _after_optional_indent(line: str, *, scan_limit: int) -> int:
    start = 0
    while start < scan_limit and start < 3 and line[start] == " ":
        start += 1
    return start


def _run_end(line: str, *, start: int, limit: int) -> int:
    character = line[start]
    end = start + 1
    while end < limit and line[end] == character:
        end += 1
    return end


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
    "MAX_PROMPT_HIGHLIGHT_DOCUMENT_CHARACTERS",
    "MAX_PROMPT_HIGHLIGHT_LINE_CHARACTERS",
    "MAX_PROMPT_HIGHLIGHT_LINES",
    "MAX_PROMPT_HIGHLIGHTS_PER_DOCUMENT",
    "MAX_PROMPT_HIGHLIGHTS_PER_LINE",
    "PromptHighlight",
    "PromptHighlightKind",
    "prompt_document_highlights",
    "prompt_line_highlights",
]
