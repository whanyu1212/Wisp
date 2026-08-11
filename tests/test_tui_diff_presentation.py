"""Focused tests for structured edit/write diff-card presentation."""

from __future__ import annotations

from textual.content import Content

from wisp.tui.diff_presentation import (
    DIFF_ADD_COUNT_STYLE,
    DIFF_ADD_STYLE,
    DIFF_ADD_TOKEN_STYLE,
    DIFF_CONTEXT_STYLE,
    DIFF_DEL_COUNT_STYLE,
    DIFF_DEL_STYLE,
    DIFF_DEL_TOKEN_STYLE,
    DIFF_EXPANDED_BYTES,
    DIFF_META_STYLE,
    DiffOperation,
    DiffRow,
    DiffRowKind,
    DiffVisibleRow,
    select_diff_rows,
)
from wisp.tui.tool_output import build_edit_diff_presentation, build_write_diff_presentation
from wisp.tui.widgets import _render_diff_presentation, _render_diff_visible_row


def _edit(old: str, new: str) -> dict[str, object]:
    return {"path": "pkg/example.py", "edits": [{"oldText": old, "newText": new}]}


def _styles_at(content: Content, needle: str) -> set[str]:
    start = content.plain.index(needle)
    return {str(span.style) for span in content.spans if span.start <= start < span.end}


def test_changed_row_styles_carry_a_row_tint_and_a_stronger_token_tint() -> None:
    # Changed rows are painted as bands so coverage is uniform across every
    # addition and deletion, including those with no line pairing to derive
    # token emphasis from. The token tint is a distinct background rather than
    # a `reverse` modifier, keeping emphasis subordinate to the band.
    assert " on " in DIFF_ADD_STYLE
    assert " on " in DIFF_DEL_STYLE
    assert " on " in DIFF_ADD_TOKEN_STYLE
    assert " on " in DIFF_DEL_TOKEN_STYLE
    assert "reverse" not in DIFF_ADD_TOKEN_STYLE
    assert "reverse" not in DIFF_DEL_TOKEN_STYLE
    # A token tint must differ from its row tint, or emphasis is invisible.
    assert DIFF_ADD_TOKEN_STYLE != DIFF_ADD_STYLE
    assert DIFF_DEL_TOKEN_STYLE != DIFF_DEL_STYLE


def test_unchanged_and_metadata_rows_take_no_background() -> None:
    # Only changed rows are banded; a tint on context or metadata would make
    # the whole card read as one block and destroy the add/delete signal.
    assert " on " not in DIFF_CONTEXT_STYLE
    assert " on " not in DIFF_META_STYLE


def test_header_counts_take_the_diff_hues_without_a_band() -> None:
    # The +N/-N summary sits outside the diff body, where a tinted rectangle
    # would read as a stray row.
    assert " on " not in DIFF_ADD_COUNT_STYLE
    assert " on " not in DIFF_DEL_COUNT_STYLE


def test_structured_diff_colors_counts_independently() -> None:
    presentation = build_edit_diff_presentation(_edit("old\n", "new\n"))

    assert presentation is not None
    content = _render_diff_presentation(presentation, width=80, expanded=False)
    assert DIFF_ADD_COUNT_STYLE in _styles_at(content, "+1")
    assert DIFF_DEL_COUNT_STYLE in _styles_at(content, "-1")


def test_structured_context_is_muted_without_trailing_fill() -> None:
    content = _render_diff_visible_row(
        DiffVisibleRow(DiffRow(DiffRowKind.context, "keep")),
        width=40,
        show_line_numbers=False,
    )

    assert content.plain.endswith("keep")
    assert len(content.plain) < 40
    assert DIFF_CONTEXT_STYLE in _styles_at(content, "keep")


def test_changed_rows_pad_to_full_width_so_the_tint_forms_a_band() -> None:
    # Without the fill, a row tint would stop at the end of the source text and
    # read as a ragged coloured fragment rather than a band.
    for kind, style in (
        (DiffRowKind.addition, DIFF_ADD_STYLE),
        (DiffRowKind.deletion, DIFF_DEL_STYLE),
    ):
        content = _render_diff_visible_row(
            DiffVisibleRow(DiffRow(kind, "short")),
            width=40,
            show_line_numbers=False,
        )

        # Two leading indent cells sit outside the banded region.
        assert len(content.plain) == 40 + 2
        assert content.plain.endswith(" ")
        # The trailing fill carries the row tint, not a bare unstyled gap.
        assert style in _styles_at(content, content.plain[-1])


def test_changed_rows_are_not_padded_past_the_available_width() -> None:
    # A row whose source already fills the width must not overflow the card.
    source = "x" * 60
    content = _render_diff_visible_row(
        DiffVisibleRow(DiffRow(DiffRowKind.addition, source)),
        width=40,
        show_line_numbers=False,
    )

    assert len(content.plain) == 40 + 2


def test_edit_presentation_preserves_literal_rows_and_line_positions() -> None:
    presentation = build_edit_diff_presentation(_edit("keep\nold value\n", "keep\nnew value\n"))

    assert presentation is not None
    assert presentation.operation is DiffOperation.modify
    assert presentation.path == "pkg/example.py"
    assert presentation.additions == presentation.deletions == 1
    assert presentation.show_line_numbers is False
    assert [(row.kind, row.old_line, row.new_line, row.text) for row in presentation.rows] == [
        (DiffRowKind.context, None, None, "keep"),
        (DiffRowKind.deletion, None, None, "old value"),
        (DiffRowKind.addition, None, None, "new value"),
    ]


def test_structured_presentation_highlights_multiline_replace_beyond_legacy_preview() -> None:
    prefix = "unchanged-" * 8
    old = "".join(f"{prefix}OLD-{index}\n" for index in range(8))
    new = "".join(f"{prefix}NEW-{index}\n" for index in range(8))

    presentation = build_edit_diff_presentation(_edit(old, new))

    assert presentation is not None
    changed_rows = [row for row in presentation.rows if row.kind is not DiffRowKind.context]
    assert len(changed_rows) == 16
    assert all(row.emphasis_ranges for row in changed_rows)


def test_collapsed_preview_balances_replacement_sides_and_expanded_reveals_more() -> None:
    old = "".join(f"old {index}\n" for index in range(20))
    new = "".join(f"new {index}\n" for index in range(20))
    presentation = build_edit_diff_presentation(_edit(old, new))

    assert presentation is not None
    collapsed = presentation.visible_rows(expanded=False)
    expanded = presentation.visible_rows(expanded=True)
    collapsed_source = [row.row for row in collapsed if row.row.is_source]

    assert [row.kind for row in collapsed_source].count(DiffRowKind.deletion) == 4
    assert [row.kind for row in collapsed_source].count(DiffRowKind.addition) == 4
    assert any(row.row.kind is DiffRowKind.omission for row in collapsed)
    assert presentation.can_expand
    assert len([row for row in expanded if row.row.is_source]) == 40
    assert not any(row.row.kind is DiffRowKind.omission for row in expanded)


def test_expanded_selection_remains_bounded_and_marks_remaining_evidence() -> None:
    old = "".join(f"old {index}\n" for index in range(250))
    new = "".join(f"new {index}\n" for index in range(250))
    presentation = build_edit_diff_presentation(_edit(old, new))

    assert presentation is not None
    expanded = presentation.visible_rows(expanded=True)
    source_rows = [row for row in expanded if row.row.is_source]

    assert len(source_rows) == 400
    assert presentation.additions == presentation.deletions == 250
    assert any(row.row.kind is DiffRowKind.omission for row in expanded)
    assert "lines hidden" in "\n".join(row.row.text for row in expanded)
    # A mounted card retains the same bounded expanded evidence, not all 500
    # deleted and 500 added source rows used transiently to derive the diff.
    assert sum(row.is_source for row in presentation.rows) == 400
    assert any(row.kind is DiffRowKind.omission for row in presentation.rows)


def test_retained_expanded_omissions_survive_later_collapsed_byte_clipping() -> None:
    old = "x" * 3_000 + "\n" + "".join(f"old {index}\n" for index in range(249))
    new = "y" * 3_000 + "\n" + "".join(f"new {index}\n" for index in range(249))
    presentation = build_edit_diff_presentation(_edit(old, new))

    assert presentation is not None
    visible = presentation.visible_rows(expanded=False)
    final_omission = visible[-1]
    total_bytes = sum(len(row.text.encode("utf-8")) for row in presentation.rows if row.is_source)
    shown_bytes = sum(len(row.row.text.encode("utf-8")) for row in visible if row.row.is_source)

    assert final_omission.row.kind is DiffRowKind.omission
    # The retained outer 400-row bound hides 100 rows; a later 2-KiB collapsed
    # byte clip must aggregate that metadata rather than report only retained rows.
    assert final_omission.hidden_rows == 500
    assert final_omission.hidden_bytes > total_bytes - shown_bytes


def test_nested_selection_does_not_double_count_pending_partial_source() -> None:
    rows = (
        DiffRow(DiffRowKind.deletion, "shown"),
        DiffRow(DiffRowKind.deletion, "prefix", hidden_rows=1, hidden_bytes=10),
        DiffRow(
            DiffRowKind.omission, "… 1 line hidden, 10 bytes hidden", hidden_rows=1, hidden_bytes=10
        ),
    )

    visible = select_diff_rows(rows, max_rows=1, max_bytes=100)
    omission = visible[-1]

    assert omission.row.kind is DiffRowKind.omission
    assert omission.hidden_rows == 1
    assert omission.hidden_bytes == len(b"prefix") + 10


def test_partial_retained_source_does_not_double_count_its_omission() -> None:
    presentation = build_write_diff_presentation(
        None,
        {"path": "large.py", "content": "x" * (DIFF_EXPANDED_BYTES + 1_000)},
        created=True,
    )

    assert presentation is not None
    retained_source = next(row for row in presentation.rows if row.is_source)
    assert retained_source.terminator_note_length == 0
    collapsed = presentation.visible_rows(expanded=False)
    final_omission = collapsed[-1]

    assert final_omission.row.kind is DiffRowKind.omission
    # The retained expanded prefix and its later omission describe one source
    # line, not two independent hidden lines.
    assert final_omission.hidden_rows == 1


def test_create_presentation_has_only_addition_rows() -> None:
    presentation = build_write_diff_presentation(
        None,
        {"path": "new.py", "content": "one\ntwo\n"},
        created=True,
    )

    assert presentation is not None
    assert presentation.operation is DiffOperation.create
    assert presentation.additions == 2
    assert presentation.deletions == 0
    assert presentation.show_line_numbers is True
    assert [row.kind for row in presentation.rows] == [
        DiffRowKind.hunk,
        DiffRowKind.addition,
        DiffRowKind.addition,
    ]


def test_byte_clipping_counts_preselected_omissions_in_its_final_marker() -> None:
    rows = (
        DiffRow(DiffRowKind.hunk, "@@ -1,20 +1,20 @@"),
        *(
            DiffRow(DiffRowKind.deletion, "x" * 3_000 if index == 0 else f"old {index}")
            for index in range(20)
        ),
        *(DiffRow(DiffRowKind.addition, f"new {index}") for index in range(20)),
    )

    visible = select_diff_rows(rows, max_rows=8, max_bytes=32)
    omission = visible[-1]
    total_bytes = sum(len(row.text.encode("utf-8")) for row in rows if row.is_source)

    displayed_source_bytes = sum(
        len(visible_row.row.text.encode("utf-8"))
        for visible_row in visible
        if visible_row.row.is_source
    )

    assert omission.row.kind is DiffRowKind.omission
    assert omission.hidden_rows == 40
    assert omission.hidden_bytes == total_bytes - displayed_source_bytes


def test_selection_clips_utf8_on_a_character_boundary_and_reports_the_remainder() -> None:
    presentation = build_edit_diff_presentation(_edit("before\n", "☃" * 3000 + "\n"))

    assert presentation is not None
    visible = select_diff_rows(presentation.rows, max_rows=8, max_bytes=32)
    text = "\n".join(row.row.text for row in visible)

    assert "�" not in text
    assert any(row.row.kind is DiffRowKind.omission for row in visible)
    assert "bytes hidden" in text
