"""Unit tests for tool-aware success summaries (issue #74 PR C).

`summarize_tool_result` builds a concise one-liner for read-type tools from the
structured facts in their `ToolResult.data` — never by parsing the output text — and
returns None for any tool without a summary. These feed the promoted `summary` event
field the TUI renders in place of a raw output dump.
"""

from __future__ import annotations

from wisp.tools.summary import (
    _PATH_MAX_CHARS,
    _SUMMARY_MAX_CHARS,
    summarize_tool_result,
)

# --- read --------------------------------------------------------------------


def _read_data(
    *, line_count: int, selected_count: int, path: str | None = "foo.py"
) -> dict[str, object]:
    # Truncation is NOT part of data — read gets it via the summarize() parameter
    # (sourced from ToolResult.truncated), so tests pass truncated= separately.
    data: dict[str, object] = {
        "line_count": line_count,
        "selected_count": selected_count,
    }
    if path is not None:
        data["path"] = path
    return data


def test_read_summary_whole_file() -> None:
    # A full read (slice == file, not truncated) reports the simple count.
    assert (
        summarize_tool_result("read", _read_data(line_count=42, selected_count=42))
        == "read 42 lines from foo.py"
    )


def test_read_summary_singular_line() -> None:
    assert (
        summarize_tool_result("read", _read_data(line_count=1, selected_count=1, path="x.py"))
        == "read 1 line from x.py"
    )


def test_read_summary_without_path() -> None:
    assert (
        summarize_tool_result("read", _read_data(line_count=5, selected_count=5, path=None))
        == "read 5 lines"
    )


def test_read_summary_paged_slice_reports_returned_of_total() -> None:
    # The P1 case: a small page of a large file must report what was RETURNED and the
    # file total, never the whole-file count as if it were all read.
    assert (
        summarize_tool_result(
            "read", _read_data(line_count=10000, selected_count=2, path="large.log")
        )
        == "read 2 lines of 10000 from large.log"
    )


def test_read_summary_truncated_does_not_overstate_count() -> None:
    # When the output budget clipped the slice, fewer lines were returned than the
    # slice size, so no exact count is honest: report truncation + file total.
    assert (
        summarize_tool_result(
            "read",
            _read_data(line_count=500, selected_count=500, path="big.txt"),
            truncated=True,
        )
        == "read (truncated) from big.txt — 500 lines total"
    )


def test_read_summary_falls_back_when_selected_count_absent() -> None:
    # Older data shape without selected_count degrades to the whole-file count rather
    # than returning None.
    assert (
        summarize_tool_result("read", {"path": "x.py", "line_count": 7}) == "read 7 lines from x.py"
    )


def test_read_summary_none_when_line_count_missing_or_non_int() -> None:
    assert summarize_tool_result("read", {"path": "x"}) is None
    assert summarize_tool_result("read", {"line_count": "many"}) is None


# --- grep --------------------------------------------------------------------


def test_grep_summary_counts_matches() -> None:
    assert (
        summarize_tool_result("grep", {"count": 3, "matches": ["a", "b", "c"]}) == "grep: 3 matches"
    )


def test_grep_summary_singular_match() -> None:
    assert summarize_tool_result("grep", {"count": 1, "matches": ["a"]}) == "grep: 1 match"


def test_grep_summary_zero_matches() -> None:
    assert summarize_tool_result("grep", {"count": 0, "matches": []}) == "grep: no matches"


def test_grep_summary_marks_truncated_results() -> None:
    # A capped grep (count itself limited by max_results, or output-budget clipped)
    # must carry a "there's more" cue — the summary replaces the raw [truncated]
    # marker, so dropping it would hide that results were omitted.
    assert (
        summarize_tool_result("grep", {"count": 100, "matches": []}, truncated=True)
        == "grep: 100 matches (+ more)"
    )


def test_grep_summary_none_when_count_missing() -> None:
    assert summarize_tool_result("grep", {"matches": []}) is None


# --- find --------------------------------------------------------------------


def test_find_summary_counts_files() -> None:
    assert summarize_tool_result("find", {"count": 7, "files": []}) == "find: 7 files"


def test_find_summary_singular_and_zero() -> None:
    assert summarize_tool_result("find", {"count": 1, "files": ["x"]}) == "find: 1 file"
    assert summarize_tool_result("find", {"count": 0, "files": []}) == "find: no files"


def test_find_summary_marks_truncated_results() -> None:
    assert (
        summarize_tool_result("find", {"count": 50, "files": []}, truncated=True)
        == "find: 50 files (+ more)"
    )


# --- ls ----------------------------------------------------------------------


def test_ls_summary_counts_entries_with_path() -> None:
    assert (
        summarize_tool_result("ls", {"path": "src/", "entries": ["a", "b", "c"]})
        == "ls: 3 entries in src/"
    )


def test_ls_summary_singular_entry() -> None:
    assert summarize_tool_result("ls", {"path": "d", "entries": ["only"]}) == "ls: 1 entry in d"


def test_ls_summary_empty_directory() -> None:
    assert summarize_tool_result("ls", {"path": "src/", "entries": []}) == "ls: empty (src/)"


def test_ls_summary_marks_truncated_as_a_floor() -> None:
    # A truncated ls reports the kept entries as a floor, not the true total.
    assert (
        summarize_tool_result("ls", {"path": "big/", "entries": ["a", "b"]}, truncated=True)
        == "ls: 2 entries in big/ (+ more)"
    )


def test_ls_summary_none_when_entries_not_a_list() -> None:
    assert summarize_tool_result("ls", {"path": "p", "entries": "nope"}) is None


# --- gating & bounds ---------------------------------------------------------


def test_summary_none_for_tools_without_one() -> None:
    # Diff/shell tools and unknown tools have no summary — they render their own way.
    assert summarize_tool_result("edit", {"edits": 1}) is None
    assert summarize_tool_result("write", {"bytes": 5, "created": True}) is None
    assert summarize_tool_result("bash", {"exit_code": 0}) is None
    assert summarize_tool_result("custom", {"count": 3}) is None


def test_summary_is_bounded_at_the_source() -> None:
    # The summary rides the RPC wire, so a pathological long path can't produce an
    # unbounded summary — it is clipped to the ceiling.
    summary = summarize_tool_result("read", {"path": "a/" * 5000 + "end.py", "line_count": 3})
    assert summary is not None
    assert len(summary) <= _SUMMARY_MAX_CHARS


def test_long_path_is_middle_clipped_keeping_head_and_tail() -> None:
    # A long path is middle-truncated so both the leading segment and the filename
    # survive — the most useful parts for identifying the file.
    long_path = "aaaa/" * 40 + "target.py"
    summary = summarize_tool_result("read", {"path": long_path, "line_count": 3})
    assert summary is not None
    assert summary.startswith("read 3 lines from aaaa/")
    assert summary.endswith("target.py")
    assert "…" in summary
    # The interpolated path itself stayed within its own ceiling.
    assert len(summary) <= len("read 3 lines from ") + _PATH_MAX_CHARS
