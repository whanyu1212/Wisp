"""Focused tests for structured grep/find/read transcript presentation."""

from __future__ import annotations

from wisp.tui.file_result_presentation import (
    build_file_result_presentation,
    render_file_result_presentation,
)


def test_grep_groups_rows_by_file_and_distinguishes_context() -> None:
    presentation = build_file_result_presentation(
        "grep",
        {"pattern": "TODO"},
        "src/a.py-9-before\nsrc/a.py:10:TODO first\n--\nsrc/b.py:4:TODO second",
        "grep: 2 matches",
    )

    assert presentation is not None
    assert [group.path for group in presentation.groups] == ["src/a.py", "src/b.py"]
    assert [row.kind for row in presentation.groups[0].rows] == ["context", "match"]
    assert presentation.groups[0].match_count == 1
    assert presentation.groups[0].rows[1].highlight_ranges == ((0, 4),)

    collapsed = render_file_result_presentation(presentation, width=80, expanded=False)
    expanded = render_file_result_presentation(presentation, width=80, expanded=True)
    assert collapsed.plain == "grep: 2 matches"
    assert "src/a.py · 1 match\n 9 │ before\n10 │ TODO first" in expanded.plain
    assert "src/b.py · 1 match\n4 │ TODO second" in expanded.plain
    todo_start = expanded.plain.index("TODO first")
    assert any(
        span.start <= todo_start < span.end and str(span.style) == "bold $accent"
        for span in expanded.spans
    )


def test_grep_does_not_re_evaluate_regex_for_highlighting() -> None:
    presentation = build_file_result_presentation(
        "grep",
        {"pattern": "T.DO"},
        "src/a.py:10:TODO",
        "grep: 1 match",
    )

    assert presentation is not None
    assert presentation.groups[0].rows[0].highlight_ranges == ()


def test_grep_ambiguous_flat_record_uses_generic_fallback() -> None:
    presentation = build_file_result_presentation(
        "grep",
        {"pattern": "value"},
        "src/a.py:10:value:12:also looks like a location",
        "grep: 1 match",
    )

    assert presentation is None


def test_find_uses_two_columns_only_when_the_width_can_hold_them() -> None:
    presentation = build_file_result_presentation(
        "find",
        {"pattern": "*.py"},
        "src/a.py\nsrc/b.py\ntests/a.py\ntests/b.py",
        "find: 4 files",
    )

    assert presentation is not None
    narrow = render_file_result_presentation(presentation, width=40, expanded=True)
    wide = render_file_result_presentation(presentation, width=100, expanded=True)
    assert narrow.plain.splitlines()[1:] == [
        "src/a.py",
        "src/b.py",
        "tests/a.py",
        "tests/b.py",
    ]
    assert len(wide.plain.splitlines()) == 3  # summary plus two paired rows
    assert "src/a.py" in wide.plain and "tests/a.py" in wide.plain


def test_read_uses_requested_offset_for_line_number_gutter() -> None:
    presentation = build_file_result_presentation(
        "read",
        {"path": "src/app.py", "offset": 20},
        "first\nsecond\n",
        "read 2 lines of 40 from src/app.py",
    )

    assert presentation is not None
    expanded = render_file_result_presentation(presentation, width=80, expanded=True)
    assert "src/app.py\n20 │ first\n21 │ second" in expanded.plain


def test_truncated_result_reports_only_counts_known_from_summary() -> None:
    presentation = build_file_result_presentation(
        "grep",
        {"pattern": "match"},
        "src/a.py:1:match\nsrc/a.py:2:match\n[truncated]",
        "grep: 5 matches (+ more)",
        truncated=True,
    )

    assert presentation is not None
    expanded = render_file_result_presentation(presentation, width=80, expanded=True)
    assert expanded.plain.endswith("… at least 3 more matches")


def test_markup_like_file_content_remains_literal() -> None:
    presentation = build_file_result_presentation(
        "read",
        {"path": "[red]file.py[/red]"},
        "[red]literal[/red]",
        "read 1 line from [red]file.py[/red]",
    )

    assert presentation is not None
    rendered = render_file_result_presentation(presentation, width=80, expanded=True)
    assert "[red]literal[/red]" in rendered.plain
    assert all("red" not in str(span.style).lower() for span in rendered.spans)
