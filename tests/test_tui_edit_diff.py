"""Unit tests for the edit-diff renderer (issue #74 PR B1).

These exercise `render_edit_diff` and its helpers in isolation, feeding the REAL
`edit` tool argument shape (`{"edits": [{"oldText", "newText"}]}`) rather than
placeholders. The renderer returns a Textual `Content` whose file text is literal
(never parsed as markup) and whose add/delete lines carry theme-styled spans.
"""

from __future__ import annotations

from textual.content import Content

from wisp.tui.tool_output import (
    _DIFF_ADD_STYLE,
    _DIFF_DEL_STYLE,
    _DIFF_MAX_HUNK_LINES,
    _DIFF_MAX_TOTAL_LINES,
    _DIFF_META_STYLE,
    _DIFF_PREVIEW_BYTES,
    _DIFF_PREVIEW_LINES,
    _clip_line_to_bytes,
    _unified_diff_lines,
    render_edit_diff,
    render_tool_result,
)


def _edit(*hunks: tuple[str, str], path: str = "src/foo.py") -> dict[str, object]:
    return {
        "path": path,
        "edits": [{"oldText": old, "newText": new} for old, new in hunks],
    }


def _styles_at(content: Content, needle: str) -> set[str]:
    """Styles covering the first occurrence of ``needle`` in the content."""

    start = content.plain.index(needle)
    styles: set[str] = set()
    for span in content.spans:
        if span.start <= start < span.end:
            styles.add(str(span.style))
    return styles


def test_render_edit_diff_returns_content_with_add_and_delete() -> None:
    content = render_edit_diff(_edit(("return 1", "return 2")))
    assert isinstance(content, Content)
    assert "-return 1" in content.plain
    assert "+return 2" in content.plain


def test_render_edit_diff_colors_additions_and_deletions() -> None:
    content = render_edit_diff(_edit(("old line", "new line")))
    assert _DIFF_ADD_STYLE in _styles_at(content, "+new line")
    assert _DIFF_DEL_STYLE in _styles_at(content, "-old line")


def test_render_edit_diff_context_lines_are_unstyled() -> None:
    # A shared line between old and new is context — no add/delete color.
    content = render_edit_diff(_edit(("keep\ndrop", "keep\nadd")))
    assert _DIFF_ADD_STYLE not in _styles_at(content, " keep")
    assert _DIFF_DEL_STYLE not in _styles_at(content, " keep")


def test_render_edit_diff_hunk_header_is_meta_styled() -> None:
    content = render_edit_diff(_edit(("a", "b")))
    assert _DIFF_META_STYLE in _styles_at(content, "@@")


def test_render_edit_diff_multi_hunk_labels_each_edit() -> None:
    content = render_edit_diff(_edit(("a", "b"), ("c", "d")))
    assert "@@ edit 1 @@" in content.plain
    assert "@@ edit 2 @@" in content.plain


def test_render_edit_diff_add_only_and_delete_only() -> None:
    add_only = render_edit_diff(_edit(("", "brand new")))
    assert "+brand new" in add_only.plain
    delete_only = render_edit_diff(_edit(("gone", "")))
    assert "-gone" in delete_only.plain


def test_render_edit_diff_unicode_survives() -> None:
    content = render_edit_diff(_edit(("café ☕", "thé 🍵")))
    assert "café ☕" in content.plain
    assert "thé 🍵" in content.plain


# --- Injection safety: the security-critical property -------------------------


def test_render_edit_diff_markup_in_content_stays_literal() -> None:
    # File content containing Rich/Textual markup metacharacters MUST render as
    # literal text — never parsed as markup — so it cannot inject or escape a
    # color span. This is THE safety test.
    injected = "value [red]INJECT[/red] [/dim] [$success]x[/$success] ]["
    content = render_edit_diff(_edit(("plain", injected)))
    assert injected in content.plain  # every metacharacter survives verbatim


def test_render_edit_diff_injected_markup_carries_only_the_add_style() -> None:
    # The injected line is styled solely by the out-of-band add span; the markup
    # inside it produced no additional spans (it was never parsed).
    injected = "[red]evil[/red]"
    content = render_edit_diff(_edit(("old", injected)))
    styles = _styles_at(content, injected)
    assert styles == {_DIFF_ADD_STYLE}


# --- Defensive parsing: malformed args fall back (None) -----------------------


def test_render_edit_diff_none_on_missing_edits() -> None:
    assert render_edit_diff({"path": "x"}) is None


def test_render_edit_diff_none_on_wrong_types() -> None:
    assert render_edit_diff({"edits": "not a list"}) is None
    assert render_edit_diff({"edits": [{"oldText": 1, "newText": "x"}]}) is None
    assert render_edit_diff({"edits": [{"oldText": "x"}]}) is None  # missing newText
    assert render_edit_diff({"edits": ["not a mapping"]}) is None


def test_render_edit_diff_none_on_noop_edit() -> None:
    # Old == new for every hunk: nothing to diff, so fall back to the summary.
    assert render_edit_diff(_edit(("same", "same"))) is None


def test_render_edit_diff_none_when_every_hunk_is_noop() -> None:
    # A multi-edit call where every hunk is a no-op must fall back too — no
    # leaked "@@ edit N @@" labels with empty bodies.
    result = render_edit_diff(_edit(("same", "same"), ("also", "also")))
    assert result is None


def test_render_edit_diff_labels_only_changed_hunks() -> None:
    # A mix of real and no-op hunks labels only the ones that actually changed.
    content = render_edit_diff(_edit(("a", "b"), ("same", "same")))
    assert content is not None
    assert "@@ edit 1 @@" in content.plain
    assert "@@ edit 2 @@" not in content.plain


def test_render_edit_diff_surfaces_newline_only_change() -> None:
    # A change confined to line terminators (dropped trailing newline, CRLF→LF)
    # must still show in the diff rather than collapsing to a no-op fallback.
    dropped_newline = render_edit_diff(_edit(("a\n", "a")))
    assert dropped_newline is not None
    crlf_to_lf = render_edit_diff(_edit(("x\r\ny", "x\ny")))
    assert crlf_to_lf is not None
    # The rendered lines carry no embedded terminator — the kept newline is
    # stripped for display after difflib used it for comparison. The terminator's
    # *kind* is annotated separately (see below), so no raw CR reaches the output.
    assert "\r" not in dropped_newline.plain
    for line in dropped_newline.plain.split("\n"):
        assert not line.endswith(("\r",))


def test_render_edit_diff_newline_only_change_shows_direction() -> None:
    # Stripping the terminator for display must not erase *which* terminator
    # changed: adding a trailing newline and removing one are opposite edits and
    # must render distinctly, not both as an unexplained "-a" / "+a".
    added = render_edit_diff(_edit(("a", "a\n")))
    removed = render_edit_diff(_edit(("a\n", "a")))
    assert added is not None and removed is not None
    assert added.plain != removed.plain
    # The side lacking a newline is the one annotated: the addition's deleted
    # side (old "a" had none), the removal's added side (new "a" has none).
    assert "-a  ⏎ no newline" in added.plain
    assert "+a  ⏎ no newline" in removed.plain


def test_render_edit_diff_crlf_conversion_shows_direction() -> None:
    # CRLF→LF and LF→CRLF are opposite conversions and must render distinctly.
    crlf_to_lf = render_edit_diff(_edit(("x\r\ny", "x\ny")))
    lf_to_crlf = render_edit_diff(_edit(("x\ny", "x\r\ny")))
    assert crlf_to_lf is not None and lf_to_crlf is not None
    assert crlf_to_lf.plain != lf_to_crlf.plain
    # The CRLF side carries the CRLF marker in each direction.
    assert "-x  ⏎ CRLF" in crlf_to_lf.plain
    assert "+x  ⏎ CRLF" in lf_to_crlf.plain


def test_render_edit_diff_lone_cr_shows_direction_and_is_not_no_newline() -> None:
    # splitlines(keepends=True) treats a lone "\r" (classic-Mac line ending) as a
    # terminator, so a CR change must render distinctly and be labeled as a CR —
    # not collapse to an identical, mislabeled "no newline" on both sides.
    added_cr = render_edit_diff(_edit(("a", "a\r")))
    removed_cr = render_edit_diff(_edit(("a\r", "a")))
    assert added_cr is not None and removed_cr is not None
    assert added_cr.plain != removed_cr.plain
    # The CR side is labeled CR (the terminator it has), never "no newline".
    assert "+a  ⏎ CR" in added_cr.plain
    assert "-a  ⏎ CR" in removed_cr.plain
    # And a genuine CRLF is not misread as a lone CR.
    crlf_to_lf = render_edit_diff(_edit(("x\r\ny", "x\ny")))
    assert crlf_to_lf is not None
    assert "⏎ CRLF" in crlf_to_lf.plain
    assert "⏎ CR\n" not in crlf_to_lf.plain


def test_render_edit_diff_does_not_annotate_unchanged_context() -> None:
    # A terminator note belongs only on changed (+/-) lines. An unchanged context
    # line's terminator is identical before and after, so annotating it — even
    # when the file legitimately lacks a trailing newline — is pure noise.
    content = render_edit_diff(_edit(("a\nb\nc", "a\nB\nc")))
    assert content is not None
    # "c" is unchanged context and the file has no trailing newline, yet "c" must
    # not carry a "no newline" note; the changed lines b/B are what matter.
    assert " c  ⏎" not in content.plain
    assert " c" in content.plain


# --- Content fidelity: header-lookalike lines and the path header -------------


def test_render_edit_diff_keeps_deleted_line_that_looks_like_a_header() -> None:
    # A source line beginning with "-- " becomes "--- ..." once difflib prepends
    # its "-" delete marker — the same prefix as difflib's own file header. The
    # renderer must keep it as a body line (headers only precede the first hunk),
    # not silently drop the deletion.
    content = render_edit_diff(_edit(("-- old comment\nkeep", "-- new comment\nkeep")))
    assert content is not None
    assert "-- old comment" in content.plain  # the deleted line survives
    assert "-- new comment" in content.plain  # the added line survives
    # And the deletion is colored as a deletion, not treated as metadata.
    assert _DIFF_DEL_STYLE in _styles_at(content, "--- old comment")


def test_render_edit_diff_keeps_added_line_that_looks_like_a_header() -> None:
    # Symmetric to the delete case: a source line beginning with "++ " becomes
    # "+++ ..." after the "+" add marker and must not be mistaken for a header.
    content = render_edit_diff(_edit(("++ a", "++ b")))
    assert content is not None
    assert "++ a" in content.plain
    assert "++ b" in content.plain
    assert _DIFF_ADD_STYLE in _styles_at(content, "+++ b")


def test_render_edit_diff_leads_with_the_edited_path() -> None:
    # The diff replaces the argument summary that used to name the file, so the
    # path must reappear as a meta-styled header line.
    content = render_edit_diff(_edit(("a", "b"), path="src/pkg/module.py"))
    assert content is not None
    assert content.plain.startswith("src/pkg/module.py")
    assert _DIFF_META_STYLE in _styles_at(content, "src/pkg/module.py")


def test_render_edit_diff_omits_path_header_when_absent_or_blank() -> None:
    # A missing or non-string path just yields no header line — never a crash or
    # a stray "None" label.
    no_path = render_edit_diff({"edits": [{"oldText": "a", "newText": "b"}]})
    assert no_path is not None
    assert no_path.plain.startswith("@@")
    blank_path = render_edit_diff(_edit(("a", "b"), path=""))
    assert blank_path is not None
    assert blank_path.plain.startswith("@@")


# --- Bounding: a huge edit is capped with honest metadata ---------------------


def test_render_edit_diff_bounds_large_diff() -> None:
    old = "\n".join(f"line-{i}" for i in range(200))
    new = "\n".join(f"changed-{i}" for i in range(200))
    content = render_edit_diff(_edit((old, new)))
    line_count = content.plain.count("\n") + 1
    # The untrusted diff body is bounded to the preview budget plus the trailing
    # "... N more lines" marker; the path header is one extra, unbudgeted line
    # (a short, trusted filename that a long diff must not push out of view).
    assert line_count <= _DIFF_PREVIEW_LINES + 2
    assert "more lines" in content.plain


def test_render_edit_diff_byte_budget_counts_separators() -> None:
    # The byte budget bounds the whole rendered diff body — the newlines that join
    # kept lines included — not just the line text. This case keeps the line count
    # under the line cap so the *byte* budget is what binds, across several kept
    # lines whose text sums to just under the budget; the inter-line separators
    # are then the overflow the budget must still absorb (a body over the cap if
    # separators go uncounted).
    old = "anchor"
    new = "anchor\n" + "\n".join("z" * 490 for _ in range(4))
    content = render_edit_diff(_edit((old, new)))

    # The diff body is every line except the path header and the trailer.
    body = "\n".join(
        line for line in content.plain.split("\n") if line.startswith(("@@", "-", "+", " "))
    )
    assert len(body.encode("utf-8")) <= _DIFF_PREVIEW_BYTES
    assert "bytes hidden" in content.plain  # the pushed-out separator bytes are reported


def test_render_edit_diff_bounds_single_enormous_line() -> None:
    # A diff with just one gigantic line stays under the line cap, so a line-only
    # bound would let it through. The byte budget must still cap it and report the
    # hidden bytes honestly, or one minified/generated line floods the transcript.
    #
    # The trailer count is asserted exactly (hidden = total − kept), derived from
    # the same diff-line primitive the renderer uses, so a drift in the accounting
    # is caught — not merely that "bytes hidden" appears somewhere.
    old = "x" * 5_000
    content = render_edit_diff(_edit((old, old + "y")))

    # Total diff-line content bytes (what the renderer measures against the budget)
    # and what actually survived in the rendered body.
    total_bytes = sum(len(line.encode("utf-8")) for line in _unified_diff_lines([(old, old + "y")]))
    kept_bytes = sum(
        len(line.encode("utf-8"))
        for line in content.plain.split("\n")
        if line.startswith(("@@", "-", "+"))
    )
    assert kept_bytes <= _DIFF_PREVIEW_BYTES
    hidden_bytes = total_bytes - kept_bytes
    # The single addition line was entirely dropped (1 more line), and the clipped
    # deleted line's remaining bytes are reported exactly.
    assert f"... 1 more line, {hidden_bytes} bytes hidden" in content.plain


def test_clip_line_to_bytes_drops_partial_multibyte_char() -> None:
    # A budget landing *inside* a multibyte character must drop that partial
    # character entirely — never emit a U+FFFD replacement or invalid UTF-8.
    # "☃" is 3 bytes; a budget of 7 covers two whole snowmen (6 bytes) and one
    # byte of the third, which must be discarded.
    clipped = _clip_line_to_bytes("☃☃☃", 7)
    assert clipped == "☃☃"
    assert "�" not in clipped  # no replacement char from a mid-char cut
    assert len(clipped.encode("utf-8")) <= 7


def test_clip_line_to_bytes_returns_line_unchanged_when_it_fits() -> None:
    assert _clip_line_to_bytes("short", 100) == "short"
    # Exact-fit boundary: a line whose byte length equals the budget is kept whole.
    assert _clip_line_to_bytes("☃☃", 6) == "☃☃"


# --- Work bounding: the guard refuses to diff pathologically large input ------
#
# These assert on *work*, not display: they instrument difflib to prove an
# oversize edit never reaches the O(n*m) matcher. The display-bounding tests
# above cannot catch this — they only check the (already bounded) output, which
# is why they pass even against the pre-guard code that builds 100k diff lines to
# show 8.


def _text_with_newlines(count: int, token: str, *, sep: str = "\n") -> str:
    """A string containing exactly ``count`` line boundaries of ``sep``.

    The guard counts line boundaries (not lines), so tests express thresholds in
    boundaries directly: N joined lines carry N-1 boundaries, an off-by-one trap
    this avoids. ``sep`` defaults to LF; pass ``"\\r"`` or ``"\\r\\n"`` to exercise
    the other terminators the renderer supports.
    """

    return sep.join(f"{token}{i}" for i in range(count + 1))


def _oversize_edit(newlines: int = 60_000) -> dict[str, object]:
    """An edit whose single hunk is far over the per-hunk newline ceiling."""

    return _edit((_text_with_newlines(newlines, "old-"), _text_with_newlines(newlines, "new-")))


def test_oversize_edit_never_invokes_difflib(monkeypatch) -> None:
    # The anti-regression for this P2: an oversize edit must not call difflib at
    # all. Pre-guard, render_edit_diff calls unified_diff unconditionally and
    # materializes ~100k diff lines; here the spy would record a call.
    import wisp.tui.tool_output as mod

    calls: list[tuple] = []
    real = mod.difflib.unified_diff

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(mod.difflib, "unified_diff", spy)

    assert render_edit_diff(_oversize_edit()) is None
    assert calls == []  # the matcher was never even started


def test_oversize_edit_does_not_generate_hunk_lines(monkeypatch) -> None:
    # Bounds the number of generated diff lines directly: _single_hunk_lines must
    # never run for oversize input. Pre-guard it runs and returns ~100k lines.
    import wisp.tui.tool_output as mod

    def forbidden(*_args, **_kwargs):
        raise AssertionError("_single_hunk_lines must not run for oversize input")

    monkeypatch.setattr(mod, "_single_hunk_lines", forbidden)

    assert render_edit_diff(_oversize_edit()) is None


def test_guard_below_threshold_still_diffs(monkeypatch) -> None:
    # A hunk just under the per-hunk ceiling still produces a real diff (difflib
    # called exactly once) — the guard must not over-reject reviewable edits.
    import wisp.tui.tool_output as mod

    calls: list[tuple] = []
    real = mod.difflib.unified_diff

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(mod.difflib, "unified_diff", spy)

    # Exactly at the ceiling (not over) — the guard uses a strict `>`, so this
    # still diffs. A full replacement renders deletions first, so the 8-line
    # preview shows the leading "-" lines (the "+" additions fall past the cap).
    old = _text_with_newlines(_DIFF_MAX_HUNK_LINES, "a")
    new = _text_with_newlines(_DIFF_MAX_HUNK_LINES, "b")
    content = render_edit_diff(_edit((old, new)))
    assert content is not None
    assert "-a0" in content.plain  # a real diff was produced, not the fallback
    assert len(calls) == 1  # difflib ran exactly once


def test_guard_above_threshold_falls_back(monkeypatch) -> None:
    # A hunk one newline over the per-hunk ceiling falls back to None without
    # diffing.
    import wisp.tui.tool_output as mod

    def forbidden(*_a, **_k):
        raise AssertionError("difflib must not run above the per-hunk ceiling")

    monkeypatch.setattr(mod.difflib, "unified_diff", forbidden)

    old = _text_with_newlines(_DIFF_MAX_HUNK_LINES + 1, "a")
    new = _text_with_newlines(_DIFF_MAX_HUNK_LINES + 1, "b")
    assert render_edit_diff(_edit((old, new))) is None


def test_guard_total_lines_across_many_hunks(monkeypatch) -> None:
    # Many hunks each under the per-hunk ceiling but summing over the total
    # ceiling must also fall back — a per-hunk-only guard would let this through.
    import wisp.tui.tool_output as mod

    def forbidden(*_a, **_k):
        raise AssertionError("difflib must not run when the total ceiling is exceeded")

    monkeypatch.setattr(mod.difflib, "unified_diff", forbidden)

    # Each hunk is under the per-hunk ceiling, but three of them sum well over the
    # total ceiling (each side counts, so 3 hunks × 2 sides × half-total newlines).
    per_hunk = _DIFF_MAX_TOTAL_LINES // 2
    old = _text_with_newlines(per_hunk, "a")
    new = _text_with_newlines(per_hunk, "b")
    hunks = ((old, new), (old, new), (old, new))
    assert render_edit_diff(_edit(*hunks)) is None


def test_oversize_noop_edit_returns_none_without_diffing(monkeypatch) -> None:
    # A huge no-op edit (old == new, over the ceiling) returns None via the guard,
    # never reaching difflib. Same result as the no-op branch, but no matcher cost.
    import wisp.tui.tool_output as mod

    def forbidden(*_a, **_k):
        raise AssertionError("difflib must not run for an oversize no-op edit")

    monkeypatch.setattr(mod.difflib, "unified_diff", forbidden)

    same = _text_with_newlines(_DIFF_MAX_HUNK_LINES + 100, "line-")
    assert render_edit_diff(_edit((same, same))) is None


def test_guard_bounds_oversize_non_lf_terminators(monkeypatch) -> None:
    # The guard must count boundaries the way the diff splits them. A large
    # lone-CR (classic-Mac) or CRLF edit splits into thousands of lines via
    # splitlines, so counting only "\n" would let it start the matcher. Both
    # supported non-LF terminators must be bounded, not just LF.
    import wisp.tui.tool_output as mod

    def forbidden(*_a, **_k):
        raise AssertionError("difflib must not run for oversize non-LF input")

    monkeypatch.setattr(mod.difflib, "unified_diff", forbidden)

    for sep in ("\r", "\r\n"):
        old = _text_with_newlines(_DIFF_MAX_HUNK_LINES + 1, "o", sep=sep)
        new = _text_with_newlines(_DIFF_MAX_HUNK_LINES + 1, "n", sep=sep)
        assert render_edit_diff(_edit((old, new))) is None, f"sep={sep!r} not bounded"


def test_guard_aggregate_boundary_is_strict(monkeypatch) -> None:
    # The aggregate ceiling uses a strict ">": a total of exactly
    # _DIFF_MAX_TOTAL_LINES still diffs, one boundary more falls back. This pins
    # the boundary directly rather than jumping from 8000 to 16000.
    import wisp.tui.tool_output as mod

    calls: list[tuple] = []
    real = mod.difflib.unified_diff

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(mod.difflib, "unified_diff", spy)

    # Two hunks, each side well under the per-hunk ceiling, summing to exactly the
    # total ceiling: old+new boundaries across both hunks == _DIFF_MAX_TOTAL_LINES.
    quarter = _DIFF_MAX_TOTAL_LINES // 4
    old = _text_with_newlines(quarter, "a")
    new = _text_with_newlines(quarter, "b")
    at_ceiling = _edit((old, new), (old, new))
    assert render_edit_diff(at_ceiling) is not None  # exactly at ceiling → diffs
    assert len(calls) == 2  # difflib ran per hunk

    # One boundary over the ceiling → fall back, no diffing.
    calls.clear()
    over = _edit((_text_with_newlines(quarter + 1, "a"), new), (old, new))
    assert render_edit_diff(over) is None
    assert calls == []


def test_render_tool_result_oversize_edit_falls_back_to_generic() -> None:
    # End to end: an oversize edit through the dispatcher yields the generic
    # string preview of the output, not a diff Content and not a crash.
    result = render_tool_result(
        "edit",
        _oversize_edit(),
        "Applied 1 edit(s) to src/foo.py",
        is_error=False,
        exit_code=None,
    )
    assert isinstance(result, str)
    assert "Applied 1 edit" in result


# --- Dispatch: render_tool_result routes edit successes here ------------------


def test_render_tool_result_routes_successful_edit_to_diff() -> None:
    content = render_tool_result(
        "edit",
        _edit(("a", "b")),
        "Applied 1 edit(s) to src/foo.py",
        is_error=False,
        exit_code=None,
    )
    assert isinstance(content, Content)
    assert "-a" in content.plain and "+b" in content.plain


def test_render_tool_result_edit_failure_uses_error_not_diff() -> None:
    # A failed edit is a string error preview, not a diff Content.
    result = render_tool_result(
        "edit",
        _edit(("a", "b")),
        "edit failed: file not found",
        is_error=True,
        exit_code=None,
    )
    assert isinstance(result, str)
    assert "file not found" in result


def test_render_tool_result_malformed_edit_falls_back_to_generic() -> None:
    # Malformed edit args → generic string preview of the output, not a crash.
    result = render_tool_result(
        "edit",
        {"edits": "bad"},
        "Applied 1 edit(s) to src/foo.py",
        is_error=False,
        exit_code=None,
    )
    assert isinstance(result, str)
    assert "Applied 1 edit" in result
