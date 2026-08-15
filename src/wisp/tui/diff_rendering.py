"""Shared Textual painter for bounded structured diff rows."""

from __future__ import annotations

from rich.cells import cell_len
from textual.content import Content

from wisp.tui.diff_presentation import (
    DIFF_ADD_GUTTER_STYLE,
    DIFF_ADD_SIGN_STYLE,
    DIFF_ADD_STYLE,
    DIFF_ADD_TOKEN_STYLE,
    DIFF_CONTEXT_STYLE,
    DIFF_DEL_GUTTER_STYLE,
    DIFF_DEL_SIGN_STYLE,
    DIFF_DEL_STYLE,
    DIFF_DEL_TOKEN_STYLE,
    DIFF_GUTTER_STYLE,
    DIFF_HUNK_STYLE,
    DIFF_META_STYLE,
    DiffRow,
    DiffRowKind,
    DiffVisibleRow,
)
from wisp.tui.rendering import _truncate_to_cell_width


def render_diff_visible_row(
    visible_row: DiffVisibleRow,
    *,
    width: int,
    show_line_numbers: bool,
    indent: str = "  ",
) -> Content:
    """Paint a bounded row with semantic gutters and intraline emphasis."""

    row = visible_row.row
    if row.kind is DiffRowKind.hunk:
        return Content(indent) + Content.styled(
            _truncate_to_cell_width(row.text, width),
            DIFF_HUNK_STYLE,
        )
    if row.kind is DiffRowKind.omission:
        return Content(indent) + Content.styled(
            _truncate_to_cell_width(row.text, width),
            DIFF_META_STYLE,
        )

    marker = {
        DiffRowKind.context: " ",
        DiffRowKind.addition: "+",
        DiffRowKind.deletion: "-",
    }[row.kind]
    if show_line_numbers:
        old_line = "" if row.old_line is None else str(row.old_line)
        new_line = "" if row.new_line is None else str(row.new_line)
        numbers = f"{old_line:>4} {new_line:>4} "
    else:
        numbers = ""
    gutter_style, sign_style = _diff_gutter_styles(row)
    gutter = Content.styled(f"{indent}{numbers}", gutter_style)
    gutter += Content.styled(marker, sign_style)
    gutter += Content.styled(" │ ", gutter_style)
    source_width = max(1, width - cell_len(numbers) - cell_len(" │ ") - 1)
    source, emphasis_ranges = _crop_diff_row_source(row, width=source_width)
    row_style = _diff_row_style(row)
    content = gutter + _styled_diff_source(
        source,
        emphasis_ranges,
        _diff_token_style(row),
        row_style,
    )
    if row.kind in {DiffRowKind.addition, DiffRowKind.deletion}:
        fill = source_width - cell_len(source)
        if fill > 0:
            content += Content.styled(" " * fill, row_style)
    return content


def _diff_gutter_styles(row: DiffRow) -> tuple[str, str]:
    if row.kind is DiffRowKind.addition:
        return DIFF_ADD_GUTTER_STYLE, DIFF_ADD_SIGN_STYLE
    if row.kind is DiffRowKind.deletion:
        return DIFF_DEL_GUTTER_STYLE, DIFF_DEL_SIGN_STYLE
    return DIFF_GUTTER_STYLE, DIFF_GUTTER_STYLE


def _crop_diff_row_source(
    row: DiffRow,
    *,
    width: int,
) -> tuple[str, tuple[tuple[int, int], ...]]:
    """Crop literal source before its known synthetic terminator annotation."""

    note_length = min(max(0, row.terminator_note_length), len(row.text))
    source_text = row.text[:-note_length] if note_length else row.text
    note = row.text[-note_length:] if note_length else ""
    # Favor review evidence over an annotation when the gutter leaves too few
    # cells to show a useful changed token. At wider sizes, reserve the note's
    # exact known width and append it after the independently cropped literal.
    note_width = cell_len(note)
    source_width = width - note_width
    show_note = bool(note) and source_width >= 4
    cropped, ranges = _crop_diff_source(
        source_text,
        row.emphasis_ranges,
        width=source_width if show_note else width,
        preserve_tail=row.kind in {DiffRowKind.addition, DiffRowKind.deletion},
    )
    if show_note:
        return f"{cropped}{note}", ranges
    if note:
        # The annotation did not fit, so make the omitted metadata explicit
        # without allowing it to displace the source evidence. On an exact-fit
        # row, reserve the final cell for the marker rather than silently
        # making a newline-only change look identical on both sides.
        if cell_len(cropped) < width:
            return f"{cropped}…", ranges
        return f"{_take_cell_prefix(cropped, max(0, width - 1))}…", ranges
    return cropped, ranges


def _diff_row_style(row: DiffRow) -> str:
    if row.kind is DiffRowKind.addition:
        return DIFF_ADD_STYLE
    if row.kind is DiffRowKind.deletion:
        return DIFF_DEL_STYLE
    if row.kind is DiffRowKind.context:
        return DIFF_CONTEXT_STYLE
    return DIFF_META_STYLE


def _diff_token_style(row: DiffRow) -> str:
    """Return the stronger tint for changed tokens within a row band."""

    if row.kind is DiffRowKind.addition:
        return DIFF_ADD_TOKEN_STYLE
    if row.kind is DiffRowKind.deletion:
        return DIFF_DEL_TOKEN_STYLE
    return _diff_row_style(row)


def _crop_diff_source(
    text: str,
    ranges: tuple[tuple[int, int], ...],
    *,
    width: int,
    preserve_tail: bool,
) -> tuple[str, tuple[tuple[int, int], ...]]:
    """Crop a row while retaining changed evidence where possible."""

    if cell_len(text) <= width:
        return text, ranges
    if width < 3:
        return _truncate_to_cell_width(text, width), ()
    normalized = tuple(
        sorted(
            (max(0, start), min(len(text), end))
            for start, end in ranges
            if end > start and start < len(text)
        )
    )
    if not normalized:
        if preserve_tail and width >= 2:
            return f"…{_take_cell_suffix(text, width - 1)}", ()
        return _truncate_to_cell_width(text, width), ()

    focus_start = normalized[0][0]
    focus_end = normalized[-1][1]
    left_marker = "…" if focus_start else ""
    right_marker = "…" if focus_end < len(text) else ""
    focus_width = max(1, width - cell_len(left_marker) - cell_len(right_marker))
    focus = text[focus_start:focus_end]
    before = ""
    after = ""
    if cell_len(focus) > focus_width:
        right_marker = "…"
        focus_width = max(1, width - cell_len(left_marker) - cell_len(right_marker))
        focus = _take_cell_prefix(focus, focus_width)
        focus_end = focus_start + len(focus)
    else:
        context_width = focus_width - cell_len(focus)
        before = _take_cell_suffix(text[:focus_start], context_width // 2)
        after = _take_cell_prefix(text[focus_end:], context_width - cell_len(before))

    source = f"{left_marker}{before}{focus}{after}{right_marker}"
    offset = len(left_marker) + len(before)
    remapped = tuple(
        (offset + max(start, focus_start) - focus_start, offset + min(end, focus_end) - focus_start)
        for start, end in normalized
        if max(start, focus_start) < min(end, focus_end)
    )
    return source, remapped


def _take_cell_prefix(text: str, width: int) -> str:
    """Return the longest literal prefix that fits in ``width`` terminal cells."""

    cells = 0
    end = 0
    for index, character in enumerate(text):
        character_cells = cell_len(character)
        if cells + character_cells > width:
            break
        cells += character_cells
        end = index + 1
    return text[:end]


def _take_cell_suffix(text: str, width: int) -> str:
    """Return the longest literal suffix that fits in ``width`` terminal cells."""

    cells = 0
    start = len(text)
    for index in range(len(text) - 1, -1, -1):
        character_cells = cell_len(text[index])
        if cells + character_cells > width:
            break
        cells += character_cells
        start = index
    return text[start:]


def _styled_diff_source(
    source: str,
    ranges: tuple[tuple[int, int], ...],
    token_style: str,
    base_style: str,
) -> Content:
    """Keep literal source styled while retaining bounded intraline emphasis."""

    if not ranges:
        return Content.styled(source, base_style) if base_style else Content(source)
    content = Content("")
    cursor = 0
    for start, end in sorted(ranges):
        start = min(max(cursor, start), len(source))
        end = min(max(start, end), len(source))
        if start > cursor:
            content += (
                Content.styled(source[cursor:start], base_style)
                if base_style
                else Content(source[cursor:start])
            )
        if end > start:
            content += Content.styled(source[start:end], token_style)
        cursor = end
    if cursor < len(source):
        content += (
            Content.styled(source[cursor:], base_style) if base_style else Content(source[cursor:])
        )
    return content


__all__ = ["render_diff_visible_row"]
