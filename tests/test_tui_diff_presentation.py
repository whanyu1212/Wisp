"""Focused tests for structured edit/write diff-card presentation."""

from __future__ import annotations

from wisp.tui.diff_presentation import DiffOperation, DiffRowKind, select_diff_rows
from wisp.tui.tool_output import build_edit_diff_presentation, build_write_diff_presentation


def _edit(old: str, new: str) -> dict[str, object]:
    return {"path": "pkg/example.py", "edits": [{"oldText": old, "newText": new}]}


def test_edit_presentation_preserves_literal_rows_and_line_positions() -> None:
    presentation = build_edit_diff_presentation(_edit("keep\nold value\n", "keep\nnew value\n"))

    assert presentation is not None
    assert presentation.operation is DiffOperation.modify
    assert presentation.path == "pkg/example.py"
    assert presentation.additions == presentation.deletions == 1
    assert [(row.kind, row.old_line, row.new_line, row.text) for row in presentation.rows] == [
        (DiffRowKind.hunk, None, None, "@@ -1,2 +1,2 @@"),
        (DiffRowKind.context, 1, 1, "keep"),
        (DiffRowKind.deletion, 2, None, "old value"),
        (DiffRowKind.addition, None, 2, "new value"),
    ]


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
    assert any(row.row.kind is DiffRowKind.omission for row in expanded)
    assert "lines hidden" in "\n".join(row.row.text for row in expanded)


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
    assert [row.kind for row in presentation.rows] == [
        DiffRowKind.hunk,
        DiffRowKind.addition,
        DiffRowKind.addition,
    ]


def test_selection_clips_utf8_on_a_character_boundary_and_reports_the_remainder() -> None:
    presentation = build_edit_diff_presentation(_edit("before\n", "☃" * 3000 + "\n"))

    assert presentation is not None
    visible = select_diff_rows(presentation.rows, max_rows=8, max_bytes=32)
    text = "\n".join(row.row.text for row in visible)

    assert "�" not in text
    assert any(row.row.kind is DiffRowKind.omission for row in visible)
    assert "bytes hidden" in text
