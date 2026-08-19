"""Focused tests for structured edit/write diff-card presentation."""

from __future__ import annotations

import time

from rich.cells import cell_len
from textual.content import Content

from wisp.tui.diff_presentation import (
    DIFF_ADD_COUNT_STYLE,
    DIFF_ADD_GUTTER_STYLE,
    DIFF_ADD_SIGN_STYLE,
    DIFF_ADD_STYLE,
    DIFF_ADD_TOKEN_STYLE,
    DIFF_CONTEXT_STYLE,
    DIFF_DEL_COUNT_STYLE,
    DIFF_DEL_GUTTER_STYLE,
    DIFF_DEL_SIGN_STYLE,
    DIFF_DEL_STYLE,
    DIFF_DEL_TOKEN_STYLE,
    DIFF_EXPANDED_BYTES,
    DIFF_GUTTER_STYLE,
    DIFF_HUNK_STYLE,
    DIFF_META_STYLE,
    DiffLayout,
    DiffOperation,
    DiffPresentation,
    DiffRow,
    DiffRowKind,
    DiffVisibleRow,
    minimum_split_width,
    plan_split_diff_rows,
    resolve_diff_layout,
    select_diff_rows,
)
from wisp.tui.diff_rendering import render_diff_split_row
from wisp.tui.theme import WISP_THEME_DARK, WISP_THEME_LIGHT
from wisp.tui.tool_output import build_edit_diff_presentation, build_write_diff_presentation
from wisp.tui.widgets import _render_diff_presentation, _render_diff_visible_row


def _edit(old: str, new: str) -> dict[str, object]:
    return {"path": "pkg/example.py", "edits": [{"oldText": old, "newText": new}]}


def _styles_at(content: Content, needle: str) -> set[str]:
    start = content.plain.index(needle)
    return {str(span.style) for span in content.spans if span.start <= start < span.end}


def test_pi_diff_hues_back_theme_reactive_semantic_roles() -> None:
    assert WISP_THEME_DARK.variables["diff-add-fg"] == "#b5bd68"
    assert WISP_THEME_DARK.variables["diff-del-fg"] == "#cc6666"
    # The light green takes a minimal darker tonal adjustment to meet AA;
    # Pi's light red can remain exact.
    assert WISP_THEME_LIGHT.variables["diff-add-fg"] == "#4d754d"
    assert WISP_THEME_LIGHT.variables["diff-del-fg"] == "#aa5555"


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


def test_row_painter_separates_gutter_sign_hunk_and_source_roles() -> None:
    addition = _render_diff_visible_row(
        DiffVisibleRow(DiffRow(DiffRowKind.addition, "added", new_line=7)),
        width=40,
        show_line_numbers=True,
    )
    deletion = _render_diff_visible_row(
        DiffVisibleRow(DiffRow(DiffRowKind.deletion, "removed", old_line=3)),
        width=40,
        show_line_numbers=True,
    )
    context = _render_diff_visible_row(
        DiffVisibleRow(DiffRow(DiffRowKind.context, "kept", old_line=4, new_line=8)),
        width=40,
        show_line_numbers=True,
    )
    hunk = _render_diff_visible_row(
        DiffVisibleRow(DiffRow(DiffRowKind.hunk, "@@ -3 +7 @@")),
        width=40,
        show_line_numbers=True,
    )

    assert DIFF_ADD_GUTTER_STYLE in _styles_at(addition, "7")
    assert DIFF_ADD_SIGN_STYLE in _styles_at(addition, "+")
    assert DIFF_ADD_STYLE in _styles_at(addition, "added")
    assert DIFF_DEL_GUTTER_STYLE in _styles_at(deletion, "3")
    assert DIFF_DEL_SIGN_STYLE in _styles_at(deletion, "-")
    assert DIFF_DEL_STYLE in _styles_at(deletion, "removed")
    assert DIFF_GUTTER_STYLE in _styles_at(context, "4")
    assert DIFF_CONTEXT_STYLE in _styles_at(context, "kept")
    assert DIFF_HUNK_STYLE in _styles_at(hunk, "@@")


def test_structured_diff_colors_counts_independently() -> None:
    presentation = build_edit_diff_presentation(_edit("old\n", "new\n"))

    assert presentation is not None
    content = _render_diff_presentation(presentation, width=80, expanded=False)
    assert DIFF_ADD_COUNT_STYLE in _styles_at(content, "+1")
    assert DIFF_DEL_COUNT_STYLE in _styles_at(content, "-1")
    assert content.plain.startswith("  └ M pkg/example.py  +1 -1\n    ")


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
        assert str(content.spans[-1].style) == style


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


def test_split_planner_pairs_only_adjacent_replacements_and_keeps_unmatched_rows_blank() -> None:
    rows = tuple(
        DiffVisibleRow(row)
        for row in (
            DiffRow(DiffRowKind.hunk, "@@ -1,4 +1,4 @@"),
            DiffRow(DiffRowKind.context, "keep", old_line=1, new_line=1),
            DiffRow(DiffRowKind.deletion, "old one", old_line=2),
            DiffRow(DiffRowKind.deletion, "old two", old_line=3),
            DiffRow(DiffRowKind.addition, "new one", new_line=2),
            DiffRow(DiffRowKind.context, "middle", old_line=4, new_line=3),
            DiffRow(DiffRowKind.addition, "standalone", new_line=4),
            DiffRow(DiffRowKind.omission, "… 2 lines hidden"),
        )
    )

    planned = plan_split_diff_rows(rows)

    assert planned[0].metadata is rows[0]
    assert planned[1].left is rows[1] and planned[1].right is rows[1]
    assert planned[2].left is rows[2] and planned[2].right is rows[4]
    assert planned[3].left is rows[3] and planned[3].right is None
    assert planned[4].left is rows[5] and planned[4].right is rows[5]
    assert planned[5].left is None and planned[5].right is rows[6]
    assert planned[6].metadata is rows[7]


def test_split_planner_pairs_retained_replacement_sides_across_omission_metadata() -> None:
    rows = (
        *(DiffRow(DiffRowKind.deletion, f"old {index}") for index in range(3)),
        *(DiffRow(DiffRowKind.addition, f"new {index}") for index in range(3)),
    )
    retained = select_diff_rows(rows, max_rows=4, max_bytes=1_000)
    omissions = [row for row in retained if row.row.kind is DiffRowKind.omission]

    assert [row.row.bridges_replacement for row in omissions] == [True, False]

    planned = plan_split_diff_rows(retained)

    assert planned[0].left is retained[0] and planned[0].right is retained[3]
    assert planned[1].left is retained[1] and planned[1].right is retained[4]
    assert planned[2].metadata is retained[2]
    assert planned[3].metadata is retained[5]


def test_nested_selection_preserves_replacement_omission_provenance() -> None:
    rows = (
        *(DiffRow(DiffRowKind.deletion, f"old {index}") for index in range(3)),
        *(DiffRow(DiffRowKind.addition, f"new {index}") for index in range(3)),
    )
    retained = select_diff_rows(rows, max_rows=4, max_bytes=1_000)
    nested = select_diff_rows(
        tuple(visible_row.row for visible_row in retained),
        max_rows=2,
        max_bytes=1_000,
    )
    nested_source = [row for row in nested if row.row.is_source]
    planned = plan_split_diff_rows(nested)
    paired = next(row for row in planned if row.left is not None and row.right is not None)

    assert [row.row.kind for row in nested_source] == [
        DiffRowKind.deletion,
        DiffRowKind.addition,
    ]
    assert paired.left is nested_source[0]
    assert paired.right is nested_source[1]
    nested_omissions = [
        row.row.bridges_replacement for row in nested if row.row.kind is DiffRowKind.omission
    ]
    assert nested_omissions == [True, True, False, False]


def test_split_planner_does_not_pair_across_omitted_context() -> None:
    deletion = DiffVisibleRow(DiffRow(DiffRowKind.deletion, "old", old_line=10))
    hidden_context = DiffVisibleRow.omission(2, 20)
    addition = DiffVisibleRow(DiffRow(DiffRowKind.addition, "new", new_line=20))

    planned = plan_split_diff_rows((deletion, hidden_context, addition))

    assert planned[0].left is deletion and planned[0].right is None
    assert planned[1].metadata is hidden_context
    assert planned[2].left is None and planned[2].right is addition


def test_auto_and_explicit_split_share_conservative_width_fallback() -> None:
    presentation = DiffPresentation(
        path="example.py",
        operation=DiffOperation.modify,
        additions=1,
        deletions=1,
        rows=(),
        show_line_numbers=True,
    )
    breakpoint = minimum_split_width(show_line_numbers=True)

    assert presentation.line_number_width == 4
    assert breakpoint == 69
    assert minimum_split_width(show_line_numbers=False) == 59
    assert (
        resolve_diff_layout(DiffLayout.auto, presentation, width=breakpoint - 1)
        is DiffLayout.unified
    )
    assert (
        resolve_diff_layout(DiffLayout.split, presentation, width=breakpoint - 1)
        is DiffLayout.unified
    )
    assert resolve_diff_layout(DiffLayout.auto, presentation, width=breakpoint) is DiffLayout.split
    assert resolve_diff_layout(DiffLayout.split, presentation, width=breakpoint) is DiffLayout.split

    wide_number_presentation = DiffPresentation(
        path="large.py",
        operation=DiffOperation.modify,
        additions=1,
        deletions=1,
        rows=(
            DiffRow(DiffRowKind.deletion, "old", old_line=10_000),
            DiffRow(DiffRowKind.addition, "new", new_line=10_000),
        ),
        show_line_numbers=True,
    )
    wide_number_breakpoint = minimum_split_width(
        show_line_numbers=True,
        line_number_width=wide_number_presentation.line_number_width,
    )

    assert wide_number_presentation.line_number_width == 5
    assert wide_number_breakpoint == 71
    assert (
        resolve_diff_layout(
            DiffLayout.auto,
            wide_number_presentation,
            width=wide_number_breakpoint - 1,
        )
        is DiffLayout.unified
    )
    assert (
        resolve_diff_layout(
            DiffLayout.auto,
            wide_number_presentation,
            width=wide_number_breakpoint,
        )
        is DiffLayout.split
    )


def test_create_diff_stays_unified_in_auto_but_can_be_explicitly_split() -> None:
    presentation = DiffPresentation(
        path="new.py",
        operation=DiffOperation.create,
        additions=1,
        deletions=0,
        rows=(DiffRow(DiffRowKind.addition, "new", new_line=1),),
        show_line_numbers=True,
    )

    assert resolve_diff_layout(DiffLayout.auto, presentation, width=200) is DiffLayout.unified
    assert resolve_diff_layout(DiffLayout.split, presentation, width=200) is DiffLayout.split


def test_split_renderer_crops_unicode_by_cells_and_keeps_content_literal() -> None:
    old = DiffVisibleRow(
        DiffRow(
            DiffRowKind.deletion,
            "界" * 40 + " [bold]literal[/bold]",
            old_line=12,
            emphasis_ranges=((40, 46),),
        )
    )
    new = DiffVisibleRow(
        DiffRow(
            DiffRowKind.addition,
            "界" * 40 + " <tag>",
            new_line=12,
            emphasis_ranges=((40, 45),),
        )
    )
    planned = plan_split_diff_rows((old, new))

    content = render_diff_split_row(planned[0], width=79, show_line_numbers=True)

    assert cell_len(content.plain) == 79
    assert "…" in content.plain
    assert "[bold]" in content.plain
    assert "<tag>" in content.plain
    styles = {str(span.style) for span in content.spans}
    assert DIFF_DEL_STYLE in styles and DIFF_DEL_TOKEN_STYLE in styles
    assert DIFF_ADD_STYLE in styles and DIFF_ADD_TOKEN_STYLE in styles


def test_selection_clips_utf8_on_a_character_boundary_and_reports_the_remainder() -> None:
    presentation = build_edit_diff_presentation(_edit("before\n", "☃" * 3000 + "\n"))

    assert presentation is not None
    visible = select_diff_rows(presentation.rows, max_rows=8, max_bytes=32)
    text = "\n".join(row.row.text for row in visible)

    assert "�" not in text
    assert any(row.row.kind is DiffRowKind.omission for row in visible)
    assert "bytes hidden" in text


def test_expanded_diff_renders_in_linear_time() -> None:
    """A fully expanded diff must not fold its rows quadratically.

    ``Content.__add__`` copies the accumulated text and spans on every call, so
    appending up to ``DIFF_EXPANDED_ROWS`` rows one at a time made opening a
    large edit card stall the TUI.
    """

    def presentation(row_count: int) -> DiffPresentation:
        rows = tuple(
            DiffRow(
                kind=(
                    DiffRowKind.context,
                    DiffRowKind.addition,
                    DiffRowKind.deletion,
                )[index % 3],
                text=f"line {index} " + "z" * (index % 40),
                old_line=index + 1,
                new_line=index + 1,
            )
            for index in range(row_count)
        )
        return DiffPresentation(
            path="module.py",
            operation=DiffOperation.modify,
            additions=row_count // 3,
            deletions=row_count // 3,
            rows=rows,
            show_line_numbers=True,
        )

    def layout_duration(row_count: int) -> float:
        prepared = presentation(row_count)
        samples = []
        for _ in range(5):
            start = time.perf_counter()
            _render_diff_presentation(prepared, width=110, expanded=True)
            samples.append(time.perf_counter() - start)
        return min(samples)

    layout_duration(50)  # warm caches
    baseline = layout_duration(100)
    quadrupled = layout_duration(400)

    # Linear predicts ~4x; the quadratic version grew far past 8x.
    assert quadrupled < baseline * 8
