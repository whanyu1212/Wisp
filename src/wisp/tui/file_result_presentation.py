"""Structured, literal presentation for built-in file-oriented tool results.

The runtime deliberately keeps tool results provider-neutral and transports a
bounded text payload.  This module interprets only the stable display formats of
Wisp's own ``grep``, ``find``, and ``read`` tools so the Textual frontend can add
file grouping and line-number gutters.  Any ambiguous record falls back to the
generic literal renderer instead of guessing at source-controlled text.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import islice
from typing import Literal

from rich.cells import cell_len
from textual.content import Content

type FileResultKind = Literal["grep", "find", "read"]
type FileRowKind = Literal["match", "context", "read"]


@dataclass(frozen=True, slots=True)
class FileResultRow:
    """One literal source row with optional source location metadata."""

    line_number: int
    text: str
    kind: FileRowKind
    highlight_ranges: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class FileResultGroup:
    """Rows belonging to one display path, retained in tool-output order."""

    path: str
    rows: tuple[FileResultRow, ...]

    @property
    def match_count(self) -> int:
        return sum(row.kind == "match" for row in self.rows)


@dataclass(frozen=True, slots=True)
class FileResultPresentation:
    """Bounded file-tool output ready for width-aware Textual rendering."""

    kind: FileResultKind
    summary: str
    groups: tuple[FileResultGroup, ...] = ()
    paths: tuple[str, ...] = ()
    truncated: bool = False
    total_count: int | None = None

    @property
    def can_expand(self) -> bool:
        return bool(self.groups or self.paths)

    @property
    def expand_label(self) -> str:
        count = self.retained_count if self.truncated else self.total_count
        if self.kind == "grep":
            if self.truncated and count == 0 and self.can_expand:
                return "show context"
            noun = "match" if count == 1 else "matches"
        elif self.kind == "find":
            noun = "file" if count == 1 else "files"
        else:
            noun = "line" if count == 1 else "lines"
        return f"show {count} {noun}" if count is not None else f"show {noun}"

    @property
    def retained_count(self) -> int:
        if self.kind == "grep":
            return sum(group.match_count for group in self.groups)
        if self.kind == "find":
            return len(self.paths)
        return sum(len(group.rows) for group in self.groups)


_COUNT_RE = re.compile(
    r"^(?:grep|find): (?P<count>\d+) (?:match(?:es)?|file(?:s)?)(?: \(\+ more\))?$"
)
_GREP_RECORD_SEPARATOR_RE = re.compile(r"(?P<separator>[:-])(?P<number>\d+)(?P=separator)")
_TRUNCATION_MARKERS = frozenset(("[truncated]",))
_MAX_HIGHLIGHT_RANGES_PER_ROW = 256


def build_file_result_presentation(
    name: str,
    arguments: Mapping[str, object],
    output: str,
    summary: str,
    *,
    truncated: bool = False,
) -> FileResultPresentation | None:
    """Build a structured view for one successful built-in file tool.

    Counts come from Wisp's promoted summary. The bounded output contributes only
    presentation rows; it is never used to manufacture an authoritative count.
    """

    if name == "grep":
        return _build_grep(arguments, output, summary, truncated=truncated)
    if name == "find":
        return _build_find(output, summary, truncated=truncated)
    if name == "read":
        return _build_read(arguments, output, summary, truncated=truncated)
    return None


def render_file_result_presentation(
    presentation: FileResultPresentation,
    *,
    width: int,
    expanded: bool,
) -> Content:
    """Render a structured result literally at the card's current width."""

    summary = Content.styled(presentation.summary, "$success")
    if not expanded or not presentation.can_expand:
        return summary

    body = Content()
    if presentation.kind == "find":
        body = _render_find_paths(presentation.paths, width=width)
    else:
        body = _render_groups(presentation.groups)

    footer = _hidden_footer(presentation)
    rendered = summary
    if body.plain:
        rendered += Content("\n") + body
    if footer is not None:
        rendered += Content("\n") + Content.styled(footer, "$text-muted")
    return rendered


def _build_grep(
    arguments: Mapping[str, object],
    output: str,
    summary: str,
    *,
    truncated: bool,
) -> FileResultPresentation | None:
    if summary == "grep: no matches":
        return FileResultPresentation(
            kind="grep",
            summary=summary,
            truncated=truncated,
            total_count=0,
        )

    grouped: dict[str, list[FileResultRow]] = {}
    highlight_pattern = _grep_highlight_pattern(arguments)
    ignore_case = arguments.get("ignore_case") is True
    for line in _result_lines(output, strip_truncation_marker=truncated):
        if line == "--":
            continue
        parsed = _parse_grep_record(
            line,
            highlight_pattern=highlight_pattern,
            ignore_case=ignore_case,
        )
        if parsed is None:
            return None
        path, row = parsed
        grouped.setdefault(path, []).append(row)

    if not grouped:
        if truncated:
            return FileResultPresentation(
                kind="grep",
                summary=summary,
                truncated=True,
                total_count=_summary_count(summary),
            )
        return None
    total_count = _summary_count(summary)
    retained_match_count = sum(row.kind == "match" for rows in grouped.values() for row in rows)
    # Newlines are valid inside POSIX filenames but also delimit grep records. An
    # impossible match count signals that the flattened output is ambiguous.
    if total_count is not None and (
        retained_match_count > total_count
        or (not truncated and retained_match_count != total_count)
    ):
        return None
    return FileResultPresentation(
        kind="grep",
        summary=summary,
        groups=tuple(
            FileResultGroup(path=path, rows=tuple(rows)) for path, rows in grouped.items()
        ),
        truncated=truncated,
        total_count=total_count,
    )


def _build_find(
    output: str,
    summary: str,
    *,
    truncated: bool,
) -> FileResultPresentation | None:
    if summary == "find: no files":
        return FileResultPresentation(
            kind="find",
            summary=summary,
            truncated=truncated,
            total_count=0,
        )
    retained_paths = _result_lines(output, strip_truncation_marker=truncated)
    # Byte truncation appends its marker on a fresh line even when it cut through
    # the preceding filename. Persisted events do not record that boundary, so the
    # terminal record is not safe to present as a real path. That includes a record
    # equal to ``[truncated]``: it may be a real filename, but it may also be the
    # exact retained prefix of a longer filename.
    if truncated and retained_paths:
        retained_paths = retained_paths[:-1]
    paths = retained_paths
    if not paths:
        return None
    total_count = _summary_count(summary)
    # POSIX filenames may contain newlines, while the built-in find transport is
    # newline-delimited. A complete result whose row count disagrees with its
    # authoritative summary is therefore ambiguous and must remain literal.
    if not truncated and total_count is not None and len(paths) != total_count:
        return None
    return FileResultPresentation(
        kind="find",
        summary=summary,
        paths=paths,
        truncated=truncated,
        total_count=total_count,
    )


def _build_read(
    arguments: Mapping[str, object],
    output: str,
    summary: str,
    *,
    truncated: bool,
) -> FileResultPresentation | None:
    lines = _result_lines(
        output,
        keep_empty=True,
        strip_truncation_marker=truncated,
    )
    if not lines:
        return FileResultPresentation(
            kind="read",
            summary=summary,
            truncated=truncated,
            total_count=_read_summary_count(summary),
        )
    raw_path = arguments.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    path = raw_path
    raw_offset = arguments.get("offset")
    offset = raw_offset if type(raw_offset) is int and raw_offset > 0 else 1
    rows = tuple(
        FileResultRow(line_number=offset + index, text=line, kind="read")
        for index, line in enumerate(lines)
    )
    return FileResultPresentation(
        kind="read",
        summary=summary,
        groups=(FileResultGroup(path=path, rows=rows),),
        truncated=truncated,
        total_count=_read_summary_count(summary),
    )


def _parse_grep_record(
    line: str,
    *,
    highlight_pattern: str | None,
    ignore_case: bool,
) -> tuple[str, FileResultRow] | None:
    separators = tuple(islice(_GREP_RECORD_SEPARATOR_RE.finditer(line), 2))
    # A second ``:N:`` / ``-N-`` sequence in a filename or source line makes the
    # flattened record ambiguous. Falling back keeps us from displaying a false
    # path or line number.
    if len(separators) != 1:
        return None
    match = separators[0]
    path = line[: match.start()]
    if not path:
        return None
    number = int(match.group("number"))
    text = line[match.end() :]
    kind: FileRowKind = "match" if match.group("separator") == ":" else "context"
    ranges = (
        _literal_ranges(text, highlight_pattern, ignore_case=ignore_case)
        if kind == "match" and highlight_pattern is not None
        else ()
    )
    return path, FileResultRow(
        line_number=number,
        text=text,
        kind=kind,
        highlight_ranges=ranges,
    )


def _grep_highlight_pattern(arguments: Mapping[str, object]) -> str | None:
    pattern = arguments.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return None
    if arguments.get("literal") is True:
        return pattern
    # A regex match does not carry source ranges in the bounded result. Highlight
    # only patterns whose regex and literal meanings are identical; re-running an
    # arbitrary expression in the UI would duplicate search semantics and cost.
    return None if re.search(r"[.^$*+?{}\[\]\\|()]", pattern) else pattern


def _literal_ranges(
    text: str,
    pattern: str,
    *,
    ignore_case: bool,
) -> tuple[tuple[int, int], ...]:
    flags = re.IGNORECASE if ignore_case else 0
    matches = re.finditer(re.escape(pattern), text, flags)
    return tuple(match.span() for match in islice(matches, _MAX_HIGHLIGHT_RANGES_PER_ROW))


def _result_lines(
    output: str,
    *,
    keep_empty: bool = False,
    strip_truncation_marker: bool = False,
) -> tuple[str, ...]:
    normalized = output.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized:
        return ()
    if keep_empty:
        # Tool output preserves the source's final line terminator. Remove exactly
        # that delimiter without collapsing preceding blank source rows: ``a\n`` is
        # one row, while ``a\n\n`` is two rows and ``\n`` is one blank row.
        source = normalized[:-1] if normalized.endswith("\n") else normalized
        lines = source.split("\n")
        if strip_truncation_marker and _is_truncation_marker(lines[-1]):
            lines.pop()
        return tuple(lines)

    lines = normalized.rstrip("\n").split("\n")
    # Grep/find append this sentinel only when they report authoritative
    # truncation. Strip one terminal marker, never an identical real path or read
    # line elsewhere in the result.
    if strip_truncation_marker and lines and _is_truncation_marker(lines[-1]):
        lines.pop()
    return tuple(line for line in lines if line)


def _is_truncation_marker(line: str) -> bool:
    return bool(line) and any(marker.startswith(line) for marker in _TRUNCATION_MARKERS)


def _summary_count(summary: str) -> int | None:
    match = _COUNT_RE.fullmatch(summary)
    return int(match.group("count")) if match is not None else None


def _read_summary_count(summary: str) -> int | None:
    # Only the count immediately following Wisp's controlled prefix is safe to
    # parse. Truncated summaries put the path first, and filenames may themselves
    # contain text such as "read 999 lines".
    match = re.match(r"^read (?P<count>\d+) lines?\b", summary)
    return int(match.group("count")) if match is not None else None


def _render_groups(groups: tuple[FileResultGroup, ...]) -> Content:
    rendered_groups: list[Content] = []
    for group in groups:
        header = Content.styled(group.path, "$primary")
        if group.match_count:
            noun = "match" if group.match_count == 1 else "matches"
            header += Content.styled(f" · {group.match_count} {noun}", "$text-muted")

        gutter_width = max((len(str(row.line_number)) for row in group.rows), default=1)
        rows: list[Content] = []
        for row in group.rows:
            row_content = Content.styled(f"{row.line_number:>{gutter_width}} │ ", "$text-muted")
            row_style = "$text-muted" if row.kind == "context" else "$text"
            source = Content.styled(row.text, row_style)
            for start, end in row.highlight_ranges:
                source = source.stylize("bold $accent", start, end)
            row_content += source
            rows.append(row_content)
        group_content = header
        if rows:
            group_content += Content("\n") + Content("\n").join(rows)
        rendered_groups.append(group_content)
    return Content("\n\n").join(rendered_groups)


def _render_find_paths(paths: tuple[str, ...], *, width: int) -> Content:
    available = max(12, width - 4)
    column_gap = 3
    column_width = max(1, (available - column_gap) // 2)
    use_two_columns = (
        len(paths) > 1 and available >= 64 and all(cell_len(path) <= column_width for path in paths)
    )
    if not use_two_columns:
        return Content("\n").join(Content.styled(path, "$text") for path in paths)

    lines: list[Content] = []
    midpoint = (len(paths) + 1) // 2
    left_paths = paths[:midpoint]
    right_paths = paths[midpoint:]
    for index, left in enumerate(left_paths):
        line = Content.styled(left, "$text")
        if index < len(right_paths):
            padding = " " * (column_width - cell_len(left) + column_gap)
            line += Content(padding) + Content.styled(right_paths[index], "$text")
        lines.append(line)
    return Content("\n").join(lines)


def _hidden_footer(presentation: FileResultPresentation) -> str | None:
    if presentation.kind == "grep":
        visible = sum(group.match_count for group in presentation.groups)
        noun = "match" if presentation.total_count == visible + 1 else "matches"
    elif presentation.kind == "find":
        visible = len(presentation.paths)
        noun = "file" if presentation.total_count == visible + 1 else "files"
    else:
        visible = sum(len(group.rows) for group in presentation.groups)
        noun = "line" if presentation.total_count == visible + 1 else "lines"

    if presentation.total_count is not None and presentation.total_count > visible:
        hidden = presentation.total_count - visible
        prefix = "at least " if presentation.truncated else ""
        return f"… {prefix}{hidden} more {noun}"
    if presentation.truncated:
        return "… output truncated"
    return None


__all__ = [
    "FileResultGroup",
    "FileResultPresentation",
    "FileResultRow",
    "build_file_result_presentation",
    "render_file_result_presentation",
]
