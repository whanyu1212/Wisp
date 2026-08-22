"""Focused tests for structured grep/find/read transcript presentation."""

from __future__ import annotations

from wisp.tools.truncation import truncate_text
from wisp.tui.file_result_presentation import (
    build_file_result_presentation,
    render_file_result_presentation,
)


def test_grep_groups_rows_by_file_and_distinguishes_context() -> None:
    presentation = build_file_result_presentation(
        "grep",
        {"pattern": "TODO", "context": 1},
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


def test_grep_bounds_literal_highlights_for_repetitive_rows() -> None:
    text = "a" * 10_000
    presentation = build_file_result_presentation(
        "grep",
        {"pattern": "a", "literal": True},
        f"src/a.txt:1:{text}",
        "grep: 1 match",
    )

    assert presentation is not None
    row = presentation.groups[0].rows[0]
    assert row.text == text
    assert len(row.highlight_ranges) == 256


def test_grep_ambiguous_flat_record_uses_generic_fallback() -> None:
    presentation = build_file_result_presentation(
        "grep",
        {"pattern": "value"},
        "src/a.py:10:value:12:also looks like a location",
        "grep: 1 match",
    )

    assert presentation is None


def test_grep_unbounded_line_number_uses_generic_fallback() -> None:
    presentation = build_file_result_presentation(
        "grep",
        {"pattern": "value"},
        f"src/a.py:{'9' * 5_000}:value",
        "grep: 1 match",
    )

    assert presentation is None


def test_grep_with_newline_filename_uses_literal_fallback() -> None:
    presentation = build_file_result_presentation(
        "grep",
        {"pattern": "actual"},
        "foo:1:manufactured\nbar:2:actual",
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


def test_find_with_newline_filename_uses_literal_fallback() -> None:
    presentation = build_file_result_presentation(
        "find",
        {"pattern": "*"},
        "foo\nbar",
        "find: 1 file",
    )

    assert presentation is None


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


def test_read_without_call_metadata_does_not_fabricate_source_locations() -> None:
    presentation = build_file_result_presentation(
        "read",
        {},
        "first\nsecond\n",
        "read 2 lines from src/app.py",
    )

    assert presentation is None


def test_read_preserves_trailing_blank_source_rows() -> None:
    presentation = build_file_result_presentation(
        "read",
        {"path": "notes.txt", "offset": 7},
        "first\n\n",
        "read 2 lines from notes.txt",
    )

    assert presentation is not None
    assert [row.text for row in presentation.groups[0].rows] == ["first", ""]
    assert [row.line_number for row in presentation.groups[0].rows] == [7, 8]


def test_truncated_read_strips_only_the_synthetic_terminal_marker() -> None:
    presentation = build_file_result_presentation(
        "read",
        {"path": "notes.txt", "offset": 7},
        "first\n[truncated]\n[truncated]",
        "read 2 lines from notes.txt (truncated)",
        truncated=True,
    )

    assert presentation is not None
    assert [row.text for row in presentation.groups[0].rows] == ["first", "[truncated]"]
    assert [row.line_number for row in presentation.groups[0].rows] == [7, 8]


def test_partial_truncation_markers_are_not_presented_as_file_data() -> None:
    bounded = truncate_text("visible content", max_bytes=5, max_lines=100)
    assert bounded.text == "[trun"

    read = build_file_result_presentation(
        "read",
        {"path": "notes.txt"},
        bounded.text,
        "read 1 line from notes.txt (truncated)",
        truncated=bounded.truncated,
    )
    find = build_file_result_presentation(
        "find",
        {"pattern": "*"},
        bounded.text,
        "find: 1 file (+ more)",
        truncated=bounded.truncated,
    )
    grep = build_file_result_presentation(
        "grep",
        {"pattern": "visible"},
        bounded.text,
        "grep: 1 match (+ more)",
        truncated=bounded.truncated,
    )

    assert read is not None and read.groups == ()
    assert find is None
    assert grep is not None and grep.groups == ()
    assert grep.can_expand is False
    assert render_file_result_presentation(grep, width=80, expanded=False).plain == (
        "grep: 1 match (+ more)"
    )


def test_find_preserves_literal_truncation_marker_path_when_complete() -> None:
    complete = build_file_result_presentation(
        "find",
        {"pattern": "*"},
        "[truncated]",
        "find: 1 file",
    )

    assert complete is not None and complete.paths == ("[truncated]",)


def test_truncated_expand_label_counts_only_retained_paths() -> None:
    presentation = build_file_result_presentation(
        "find",
        {"pattern": "*"},
        "src/a.py\nsrc/b.py\n[truncated]",
        "find: 3 files (+ more)",
        truncated=True,
    )

    assert presentation is not None
    assert presentation.total_count == 3
    assert presentation.retained_count == 1
    assert presentation.expand_label == "show 1 file"


def test_truncated_find_omits_a_potentially_partial_terminal_path() -> None:
    presentation = build_file_result_presentation(
        "find",
        {"pattern": "*"},
        "src/complete.py\nsrc/partial-na\n[truncated]",
        "find: 3 files (+ more)",
        truncated=True,
    )

    assert presentation is not None
    assert presentation.paths == ("src/complete.py",)


def test_truncated_find_omits_a_marker_shaped_terminal_path() -> None:
    bounded = truncate_text("[truncated]-long-name.txt", max_bytes=23, max_lines=100)
    assert bounded.text == "[truncated]\n[truncated]"

    presentation = build_file_result_presentation(
        "find",
        {"pattern": "*"},
        bounded.text,
        "find: 1 file (+ more)",
        truncated=bounded.truncated,
    )

    assert presentation is None


def test_truncated_context_without_retained_match_uses_safe_summary() -> None:
    presentation = build_file_result_presentation(
        "grep",
        {"pattern": "match", "context": 2},
        "src/a.py-1-before\nsrc/a.py-2-nearer\n[truncated]",
        "grep: 1 match (+ more)",
        truncated=True,
    )

    assert presentation is not None and not presentation.can_expand
    assert presentation.retained_count == 0
    assert render_file_result_presentation(presentation, width=80, expanded=True).plain == (
        "grep: 1 match (+ more)"
    )


def test_grep_context_shaped_newline_filename_uses_literal_fallback() -> None:
    presentation = build_file_result_presentation(
        "grep",
        {"pattern": "actual", "context": 1},
        "foo-1-\nbar:2:actual",
        "grep: 1 match",
    )

    assert presentation is None


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
    assert expanded.plain.endswith("… at least 4 more matches")


def test_truncated_grep_omits_a_filename_shaped_terminal_record() -> None:
    presentation = build_file_result_presentation(
        "grep",
        {"pattern": "match"},
        "foo:123:bar\n[truncated]",
        "grep: 1 match (+ more)",
        truncated=True,
    )

    assert presentation is not None
    assert presentation.groups == ()
    assert presentation.can_expand is False


def test_truncated_result_uses_neutral_footer_without_hidden_count() -> None:
    presentation = build_file_result_presentation(
        "read",
        {"path": "long-line.txt"},
        "retained prefix",
        "read 1 line from long-line.txt (truncated)",
        truncated=True,
    )

    assert presentation is not None
    expanded = render_file_result_presentation(presentation, width=80, expanded=True)
    assert expanded.plain.endswith("… output truncated")
    assert "more lines" not in expanded.plain


def test_truncated_read_does_not_parse_count_from_filename() -> None:
    presentation = build_file_result_presentation(
        "read",
        {"path": "read 999 lines"},
        "only line",
        "read (truncated) from read 999 lines",
        truncated=True,
    )

    assert presentation is not None
    assert presentation.total_count is None
    expanded = render_file_result_presentation(presentation, width=80, expanded=True)
    assert expanded.plain.endswith("… output truncated")
    assert "998 more" not in expanded.plain


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
