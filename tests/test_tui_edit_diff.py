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
    _DIFF_INTRA_HIGHLIGHT_MODIFIER,
    _DIFF_MAX_HUNK_CHARS,
    _DIFF_MAX_HUNK_LINES,
    _DIFF_MAX_TOTAL_CHARS,
    _DIFF_MAX_TOTAL_LINES,
    _DIFF_META_STYLE,
    _DIFF_PREVIEW_BYTES,
    _DIFF_PREVIEW_LINES,
    _INTRA_LINE_MAX_GROUP_CHARS,
    _INTRA_LINE_MAX_GROUP_LINES,
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
    # The injected line is styled solely by the out-of-band add span (plus,
    # since "old" -> "[red]evil[/red]" is a same-line-count replace, issue
    # #111's legitimate intra-line bold variant on the changed sub-span) —
    # the markup text itself produced no additional, unexpected spans (it was
    # never parsed).
    injected = "[red]evil[/red]"
    content = render_edit_diff(_edit(("old", injected)))
    styles = {
        str(span.style)
        for span in content.spans
        if injected in content.plain[span.start : span.end]
        or content.plain[span.start : span.end] in injected
    }
    assert styles <= {_DIFF_ADD_STYLE, f"{_DIFF_ADD_STYLE} bold"}
    assert styles  # sanity: the injected text was actually styled at all


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
    # and what actually survived in the rendered body. A single changed hunk
    # (label 1, non-multi) mirrors what render_edit_diff feeds the builder.
    diff_lines, _note_lengths = _unified_diff_lines([(1, old, old + "y")], multi=False)
    total_bytes = sum(len(line.encode("utf-8")) for line in diff_lines)
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


def _text_with_lines(count: int, token: str, *, sep: str = "\n") -> str:
    """A string of exactly ``count`` lines (as ``str.splitlines`` counts them).

    The guard measures line count, so tests express thresholds in lines directly:
    ``count`` tokens joined by ``sep`` give ``count`` lines and ``count - 1``
    boundaries. ``sep`` defaults to LF; pass ``"\\r"`` or ``"\\r\\n"`` to exercise
    the other terminators the renderer supports.
    """

    return sep.join(f"{token}{i}" for i in range(count))


def _oversize_edit(lines: int = 60_000) -> dict[str, object]:
    """An edit whose single hunk is far over the per-hunk line ceiling."""

    return _edit((_text_with_lines(lines, "old-"), _text_with_lines(lines, "new-")))


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
    # The side ends in a trailing newline (an ordinary file), so the line count
    # must be exact: 4000 lines score 4000, not 4001, and stay at the ceiling.
    old = _text_with_lines(_DIFF_MAX_HUNK_LINES, "a") + "\n"
    new = _text_with_lines(_DIFF_MAX_HUNK_LINES, "b") + "\n"
    content = render_edit_diff(_edit((old, new)))
    assert content is not None
    assert "-a0" in content.plain  # a real diff was produced, not the fallback
    assert len(calls) == 1  # difflib ran exactly once


def test_guard_above_threshold_falls_back(monkeypatch) -> None:
    # A hunk one line over the per-hunk ceiling falls back to None without
    # diffing.
    import wisp.tui.tool_output as mod

    def forbidden(*_a, **_k):
        raise AssertionError("difflib must not run above the per-hunk ceiling")

    monkeypatch.setattr(mod.difflib, "unified_diff", forbidden)

    old = _text_with_lines(_DIFF_MAX_HUNK_LINES + 1, "a")
    new = _text_with_lines(_DIFF_MAX_HUNK_LINES + 1, "b")
    assert render_edit_diff(_edit((old, new))) is None


def test_guard_total_lines_across_many_hunks(monkeypatch) -> None:
    # Many hunks each under the per-hunk ceiling but summing over the total
    # ceiling must also fall back — a per-hunk-only guard would let this through.
    import wisp.tui.tool_output as mod

    def forbidden(*_a, **_k):
        raise AssertionError("difflib must not run when the total ceiling is exceeded")

    monkeypatch.setattr(mod.difflib, "unified_diff", forbidden)

    # Each hunk is under the per-hunk ceiling, but three of them sum well over the
    # total ceiling (each side counts, so 3 hunks × 2 sides × half-total lines).
    per_hunk = _DIFF_MAX_TOTAL_LINES // 2
    old = _text_with_lines(per_hunk, "a")
    new = _text_with_lines(per_hunk, "b")
    hunks = ((old, new), (old, new), (old, new))
    assert render_edit_diff(_edit(*hunks)) is None


def test_oversize_noop_edit_returns_none_without_diffing(monkeypatch) -> None:
    # A huge no-op edit (old == new, over the ceiling) returns None via the guard,
    # never reaching difflib. Same result as the no-op branch, but no matcher cost.
    import wisp.tui.tool_output as mod

    def forbidden(*_a, **_k):
        raise AssertionError("difflib must not run for an oversize no-op edit")

    monkeypatch.setattr(mod.difflib, "unified_diff", forbidden)

    same = _text_with_lines(_DIFF_MAX_HUNK_LINES + 100, "line-")
    assert render_edit_diff(_edit((same, same))) is None


def test_guard_bounds_oversize_non_lf_terminators(monkeypatch) -> None:
    # The guard must count boundaries the way the diff splits them. splitlines
    # breaks on every separator below — not just "\n" — so a large edit delimited
    # by any of them splits into thousands of lines. Counting only "\n" would let
    # a VT-, U+2028- or lone-CR-delimited whole-file rewrite start the matcher on
    # the event loop. Every splitlines separator must be bounded.
    import wisp.tui.tool_output as mod

    def forbidden(*_a, **_k):
        raise AssertionError("difflib must not run for oversize input")

    monkeypatch.setattr(mod.difflib, "unified_diff", forbidden)

    # Lone CR, CRLF, VT, FF, the C0 separators, NEL, and the Unicode line/para
    # separators — the full set str.splitlines honors.
    separators = ("\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")
    for sep in separators:
        old = _text_with_lines(_DIFF_MAX_HUNK_LINES + 1, "o", sep=sep)
        new = _text_with_lines(_DIFF_MAX_HUNK_LINES + 1, "n", sep=sep)
        assert render_edit_diff(_edit((old, new))) is None, f"sep={sep!r} not bounded"


def test_line_boundary_count_matches_splitlines() -> None:
    # The guard's boundary count must equal what splitlines produces, or the two
    # disagree about how large an input is. Assert exact agreement across every
    # separator and a mixed string, so a future separator drift is caught.
    from wisp.tui.tool_output import _line_boundary_count

    separators = (
        "\n",
        "\r",
        "\r\n",
        "\v",
        "\f",
        "\x1c",
        "\x1d",
        "\x1e",
        "\x85",
        "\u2028",
        "\u2029",
    )
    for sep in separators:
        text = f"a{sep}b{sep}c"
        assert _line_boundary_count(text) == len(text.splitlines()) - 1, f"sep={sep!r}"

    mixed = (
        "\v".join(f"a{i}" for i in range(50)) + "\u2028" + "\x85".join(f"b{i}" for i in range(50))
    )
    assert _line_boundary_count(mixed) == len(mixed.splitlines()) - 1


def test_line_count_matches_splitlines_exactly() -> None:
    # _line_count must equal len(splitlines()) exactly \u2014 a single-line hunk still
    # costs one (so many tiny hunks accumulate), and a file ending in a separator
    # is not over-counted (so an ordinary 4000-line file keeps its diff at the
    # ceiling instead of losing it a line early).
    from wisp.tui.tool_output import _line_count

    assert _line_count("") == 0  # empty side contributes nothing
    assert _line_count("one line") == 1  # a single-line hunk still costs one
    cases = (
        "x",
        "a\nb",
        "a\nb\nc",
        "a\rb",
        "trailing\n",  # a trailing separator does not add a phantom line
        "\n",
        "a\nb\n",
        "a\r\nb\r\n",
        "x\vy",
        "caf\u00e9\u2028th\u00e9",
    )
    for text in cases:
        assert _line_count(text) == len(text.splitlines()), repr(text)


def test_guard_bounds_many_no_op_hunks(monkeypatch) -> None:
    # A no-op hunk (old == new, including both-empty) produces no diff, so it must
    # be skipped BEFORE difflib \u2014 not diffed and then discarded. Otherwise a batch
    # of many no-op hunks re-opens the many-hunks work bypass: difflib once per
    # hunk. It must run zero times, and a real change after them still diffs.
    import wisp.tui.tool_output as mod

    calls: list[tuple] = []
    real = mod.difflib.unified_diff

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(mod.difflib, "unified_diff", spy)

    # Thousands of both-empty no-op hunks: difflib must never run.
    empties = tuple(("", "") for _ in range(_DIFF_MAX_TOTAL_LINES + 1))
    assert render_edit_diff(_edit(*empties)) is None
    assert calls == []

    # The same batch followed by one real change: only the real hunk is diffed.
    calls.clear()
    assert render_edit_diff(_edit(*empties, ("a", "b"))) is not None
    assert len(calls) == 1


def test_guard_survives_stateful_str_subclass(monkeypatch) -> None:
    # The no-op filter and the work guard must agree on which hunks change. A str
    # subclass with a stateful __eq__ (equal on the first compare, not after) could
    # otherwise be judged a no-op by one pass and a change by the other, charging
    # the guard zero while difflib still runs per hunk. Arguments are coerced to a
    # built-in str at the parse boundary, so the subclass never reaches the guard.
    # This subclass also overrides __str__ to return itself, so the coercion must
    # use str.__str__ (not str(), which such a subclass would pass through).
    import wisp.tui.tool_output as mod

    class FlipStr(str):
        def __new__(cls, value: str) -> FlipStr:
            obj = super().__new__(cls, value)
            obj._compares = 0  # type: ignore[attr-defined]
            return obj

        def __eq__(self, other: object) -> bool:
            self._compares += 1  # type: ignore[attr-defined]
            return self._compares == 1  # equal once (guard), then not (diff)

        def __str__(self) -> str:
            return self  # str() would not normalize this; str.__str__ must

        def __hash__(self) -> int:
            return super().__hash__()

    def forbidden(*_a, **_k):
        raise AssertionError("difflib must not run for a huge stateful-str batch")

    monkeypatch.setattr(mod.difflib, "unified_diff", forbidden)

    hunks = tuple((FlipStr(f"old {i}"), f"new {i}") for i in range(_DIFF_MAX_TOTAL_LINES + 1))
    # All are real changes once coerced; the aggregate ceiling rejects the batch.
    assert render_edit_diff(_edit(*hunks)) is None


def test_multi_hunk_label_uses_original_edit_position() -> None:
    # The "@@ edit N @@" label names the user's edit position, not a re-indexed
    # position among only the changed hunks. With two leading no-ops, the single
    # changed hunk is edit 3 — and the label shows because the call had >1 edit.
    content = render_edit_diff(_edit(("same", "same"), ("also", "also"), ("a", "b")))
    assert content is not None
    assert "@@ edit 3 @@" in content.plain
    assert "@@ edit 1 @@" not in content.plain


def test_guard_bounds_many_single_line_hunks(monkeypatch) -> None:
    # Many one-line hunks (a generated batch of replacements) each have zero
    # separators, so a boundary-only aggregate never trips. But _unified_diff_lines
    # runs difflib once PER hunk, so thousands of tiny hunks are as expensive as
    # one giant one. Charging each non-empty side at least one line makes the
    # aggregate ceiling catch this: difflib must never run.
    import wisp.tui.tool_output as mod

    def forbidden(*_a, **_k):
        raise AssertionError("difflib must not run for a huge batch of single-line hunks")

    monkeypatch.setattr(mod.difflib, "unified_diff", forbidden)

    # Each hunk is 2 lines (1 old + 1 new); enough hunks to exceed the total.
    count = _DIFF_MAX_TOTAL_LINES  # 2 lines/hunk \u00d7 this \u226b the ceiling
    hunks = tuple((f"old {i}", f"new {i}") for i in range(count))
    assert render_edit_diff(_edit(*hunks)) is None


def test_guard_bounds_single_huge_line_by_chars(monkeypatch) -> None:
    # A minified/generated file is a few enormous lines: it passes the LINE ceiling
    # (one line) but splitting it and materializing the diff-line strings allocates
    # megabytes before the display cap trims them. The per-side char ceiling must
    # reject it before difflib runs.
    import wisp.tui.tool_output as mod

    def forbidden(*_a, **_k):
        raise AssertionError("difflib must not run for a huge single-line hunk")

    monkeypatch.setattr(mod.difflib, "unified_diff", forbidden)

    big = "x" * (_DIFF_MAX_HUNK_CHARS + 1)  # one line, over the char ceiling
    assert render_edit_diff(_edit((big, big + "y"))) is None


def test_guard_bounds_aggregate_chars_across_hunks(monkeypatch) -> None:
    # Many hunks each under the per-hunk char ceiling but summing over the aggregate
    # char ceiling must also fall back \u2014 the character mirror of the aggregate-line
    # guard. Each is a single line, so the line guards never trip.
    import wisp.tui.tool_output as mod

    def forbidden(*_a, **_k):
        raise AssertionError("difflib must not run when the total char ceiling is exceeded")

    monkeypatch.setattr(mod.difflib, "unified_diff", forbidden)

    # Each side is well under _DIFF_MAX_HUNK_CHARS; a handful sum well over the
    # total char ceiling. A single line each, so the aggregate *line* guard (which
    # would only reach ~a few units here) never catches it \u2014 only the char total.
    side = "x" * (_DIFF_MAX_HUNK_CHARS // 2)
    hunk_count = (_DIFF_MAX_TOTAL_CHARS // len(side)) + 4
    hunks = tuple((side, side + "y") for _ in range(hunk_count))
    assert render_edit_diff(_edit(*hunks)) is None


def test_guard_allows_edit_just_under_char_ceilings(monkeypatch) -> None:
    # A single-line edit just under the per-hunk char ceiling (and under the
    # aggregate) still produces a real diff \u2014 the char guard must not over-reject.
    import wisp.tui.tool_output as mod

    calls: list[tuple] = []
    real = mod.difflib.unified_diff

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(mod.difflib, "unified_diff", spy)

    # Under both the per-hunk (1 side) and aggregate (old+new) char ceilings.
    side = "x" * (_DIFF_MAX_HUNK_CHARS - 100)
    content = render_edit_diff(_edit((side, side[:-1])))
    assert content is not None
    assert len(calls) == 1


def test_guard_measures_characters_not_encoded_bytes(monkeypatch) -> None:
    # The guard deliberately bounds *characters* (len), not UTF-8-encoded bytes: the
    # diff path keeps everything as Python ``str`` and never encodes the input, so
    # char count — not encoded size — is the faithful proxy for its allocation. Pin
    # that: a non-ASCII hunk whose encoded size is over the per-hunk ceiling but
    # whose character count is under it must still diff, and difflib must run.
    import wisp.tui.tool_output as mod

    calls: list[tuple] = []
    real = mod.difflib.unified_diff

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(mod.difflib, "unified_diff", spy)

    # "€" is 1 character but 3 UTF-8 bytes. Sized just under the char ceiling, its
    # encoded length is ~3x the ceiling — an encoded-byte guard would wrongly reject.
    side = "€" * (_DIFF_MAX_HUNK_CHARS - 100)
    assert len(side) < _DIFF_MAX_HUNK_CHARS
    assert len(side.encode("utf-8")) > _DIFF_MAX_HUNK_CHARS
    content = render_edit_diff(_edit((side, side[:-1])))
    assert content is not None
    assert len(calls) == 1


def test_guard_aggregate_boundary_is_strict(monkeypatch) -> None:
    # The aggregate ceiling uses a strict ">": a total of exactly
    # _DIFF_MAX_TOTAL_LINES still diffs, one line more falls back. This pins the
    # boundary directly rather than jumping from 8000 to 16000.
    import wisp.tui.tool_output as mod

    calls: list[tuple] = []
    real = mod.difflib.unified_diff

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(mod.difflib, "unified_diff", spy)

    # Two hunks, each side well under the per-hunk ceiling, summing to exactly the
    # total ceiling: old+new lines across both hunks == _DIFF_MAX_TOTAL_LINES.
    quarter = _DIFF_MAX_TOTAL_LINES // 4
    old = _text_with_lines(quarter, "a")
    new = _text_with_lines(quarter, "b")
    at_ceiling = _edit((old, new), (old, new))
    assert render_edit_diff(at_ceiling) is not None  # exactly at ceiling → diffs
    assert len(calls) == 2  # difflib ran per hunk

    # One line over the ceiling → fall back, no diffing.
    calls.clear()
    over = _edit((_text_with_lines(quarter + 1, "a"), new), (old, new))
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


# --- Intra-line highlighting: equal-length replace groups (issue #111) --------


def _bold_spans(content: Content) -> list[tuple[str, str]]:
    """(substring, style) for every span whose style carries the bold modifier."""

    return [
        (content.plain[span.start : span.end], str(span.style))
        for span in content.spans
        if _DIFF_INTRA_HIGHLIGHT_MODIFIER in str(span.style).split()
    ]


def test_render_edit_diff_equal_length_replace_highlights_changed_token() -> None:
    # A single-word change in a 1-line -> 1-line replace: the changed word is
    # bold, the unchanged surrounding text is not.
    content = render_edit_diff(_edit(("return old_value", "return new_value")))
    bold = _bold_spans(content)
    bold_text = {text for text, _ in bold}
    assert "old" in bold_text
    assert "new" in bold_text
    assert "return " not in bold_text
    assert "_value" not in bold_text


def test_render_edit_diff_equal_length_replace_highlights_both_sides() -> None:
    # Both the removed (old) and added (new) sub-spans get their own bold
    # highlight — not "new side only".
    content = render_edit_diff(_edit(("return old_value", "return new_value")))
    bold = _bold_spans(content)
    del_bold = [style for text, style in bold if text == "old"]
    add_bold = [style for text, style in bold if text == "new"]
    assert del_bold == [f"{_DIFF_DEL_STYLE} bold"]
    assert add_bold == [f"{_DIFF_ADD_STYLE} bold"]


def test_render_edit_diff_multi_line_equal_length_replace_highlights_only_changed_line() -> None:
    content = render_edit_diff(_edit(("aaa\nbbb\nccc", "aaa\nBBB\nccc")))
    bold_text = {text for text, _ in _bold_spans(content)}
    assert bold_text == {"bbb", "BBB"}


def test_render_edit_diff_unequal_length_replace_has_no_intra_line_highlight() -> None:
    # A replace where old/new have different line counts must fall back to
    # whole-line-only styling — no bold anywhere, matching toad's own guard.
    content = render_edit_diff(_edit(("one line", "one line\nanother line")))
    assert _bold_spans(content) == []
    styles = {str(span.style) for span in content.spans}
    assert styles <= {_DIFF_ADD_STYLE, _DIFF_DEL_STYLE, _DIFF_META_STYLE, ""}


def test_render_edit_diff_insert_only_has_no_intra_line_highlight() -> None:
    content = render_edit_diff(_edit(("keep", "keep\nadded")))
    assert _bold_spans(content) == []


def test_render_edit_diff_delete_only_has_no_intra_line_highlight() -> None:
    content = render_edit_diff(_edit(("keep\nremoved", "keep")))
    assert _bold_spans(content) == []


def test_render_edit_diff_adjacent_replace_groups_with_different_lengths_do_not_merge() -> None:
    # Two back-to-back replace-groups with no context line between them, one
    # equal-length and one not: difflib's own get_grouped_opcodes would merge
    # these into a single unequal-length replace, so NEITHER gets intra-line
    # highlighting — a documented, accepted conservative trade-off, not a bug.
    old = "aaa\nx1\nx2"
    new = "aaa\ny1\ny2\ny3"
    content = render_edit_diff(_edit((old, new)))
    assert _bold_spans(content) == []


def test_render_edit_diff_intra_line_highlight_survives_terminator_note() -> None:
    # old has no trailing newline (annotated "no newline"); the intra-line
    # highlight must never touch that annotation's text, only the real content.
    content = render_edit_diff(_edit(("hello", "hallo\n")))
    for text, _ in _bold_spans(content):
        assert "⏎" not in text
    bold_text = {text for text, _ in _bold_spans(content)}
    assert bold_text == {"e", "a"}


def test_render_edit_diff_intra_line_highlight_covers_text_matching_a_note_spelling() -> None:
    # P3 regression guard: real file content that happens to end with the
    # literal text of a terminator-note annotation (e.g. a line genuinely
    # ending in "  ⏎ CRLF", both sides properly LF-terminated so no
    # annotation is actually appended by _terminator_note here) must still
    # be compared and highlighted like any other content — not silently
    # stripped because it pattern-matches the annotation vocabulary. Both
    # lines end with a real "\n", so _terminator_note appends nothing; the
    # trailing "  ⏎ CRLF" text is genuine file content that changed.
    old = "status  ⏎ CRLF\n"
    new = "status\n"
    content = render_edit_diff(_edit((old, new)))
    bold_text = {text for text, _ in _bold_spans(content)}
    assert bold_text == {"  ⏎ CRLF"}


def test_intra_line_highlight_runs_below_its_ceiling(monkeypatch) -> None:
    # A group comfortably under the per-group ceiling gets highlighted — the
    # guard must not over-reject a normal small edit.
    import wisp.tui.tool_output as mod

    calls: list[tuple] = []
    real = mod.difflib.SequenceMatcher

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(mod.difflib, "SequenceMatcher", spy)

    content = render_edit_diff(_edit(("return old_value", "return new_value")))
    # difflib.unified_diff itself calls SequenceMatcher once internally (the
    # line-level diff), so one equal-length replace-group hunk costs exactly
    # two SequenceMatcher calls total: one from unified_diff, one from
    # _intra_line_ranges_for_group's own char-level pass.
    assert len(calls) == 2
    assert _bold_spans(content) != []


def test_intra_line_ranges_for_group_respects_own_size_ceiling(monkeypatch) -> None:
    # A group at/over the per-group line ceiling must return None — the
    # caller then falls back to whole-line-only styling for that group.
    # Exercises _intra_line_ranges_for_group directly rather than through the
    # full render_edit_diff pipeline: at _INTRA_LINE_MAX_GROUP_LINES's actual
    # value, a group that large would already be cut short by the much
    # smaller _DIFF_PREVIEW_LINES preview window before _equal_length_replace
    # _groups could ever see it as a complete, equal-count group — this test
    # isolates the size-ceiling guard itself from that separate, tighter
    # preview-window guard (see test_intra_line_highlight_skips_groups_past
    # _the_preview_window / ..._never_scans_a_group_entirely_past_the_preview
    # for that one).
    import wisp.tui.tool_output as mod

    calls: list[tuple] = []
    real_matcher = mod.difflib.SequenceMatcher

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_matcher(*args, **kwargs)

    monkeypatch.setattr(mod.difflib, "SequenceMatcher", spy)

    old_lines = [f"old-{i}" for i in range(_INTRA_LINE_MAX_GROUP_LINES + 1)]
    new_lines = [f"new-{i}" for i in range(_INTRA_LINE_MAX_GROUP_LINES + 1)]
    result = mod._intra_line_ranges_for_group(old_lines, new_lines)

    assert result is None
    assert calls == []  # the matcher was never even started


def test_render_edit_diff_large_group_falls_back_to_whole_line_via_preview_cutoff() -> None:
    # End-to-end confirmation that an oversized equal-length replace group
    # still renders as a real, whole-line-only diff (not None, not a crash) —
    # in practice via the preview-window guard rather than the size ceiling,
    # since the former is tighter for a single-hunk fixture at this scale.
    old = _text_with_lines(_INTRA_LINE_MAX_GROUP_LINES + 1, "old-")
    new = _text_with_lines(_INTRA_LINE_MAX_GROUP_LINES + 1, "new-")
    content = render_edit_diff(_edit((old, new)))

    assert content is not None
    assert _bold_spans(content) == []
    assert "old-0" in content.plain


def test_intra_line_highlight_respects_own_char_ceiling(monkeypatch) -> None:
    import wisp.tui.tool_output as mod

    calls: list[tuple] = []
    real = mod._intra_line_ranges_for_group

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(mod, "_intra_line_ranges_for_group", spy)

    # A single line whose length alone exceeds the char ceiling.
    old = "a" * (_INTRA_LINE_MAX_GROUP_CHARS + 1)
    new = "b" * (_INTRA_LINE_MAX_GROUP_CHARS + 1)
    content = render_edit_diff(_edit((old, new)))

    assert len(calls) == 1
    assert content is not None
    assert _bold_spans(content) == []


def test_intra_line_highlight_skips_groups_past_the_preview_window(monkeypatch) -> None:
    # P2 regression guard: intra-line matching must never run for a
    # replace-group entirely beyond diff_lines[:_DIFF_PREVIEW_LINES] —
    # _content_from_diff_lines never renders those lines, so computing
    # ranges for them is wasted SequenceMatcher work a guard-admitted-but-
    # hidden edit could otherwise use to block the TUI. Many small,
    # independent equal-length replace-groups (separated by context lines so
    # they don't merge into one big group), only the first of which falls
    # inside the preview window.
    import wisp.tui.tool_output as mod

    calls: list[tuple] = []
    real = mod._intra_line_ranges_for_group

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(mod, "_intra_line_ranges_for_group", spy)

    hunk_pairs = [
        (f"context{i}\nold_word{i}\nmore_context{i}", f"context{i}\nnew_word{i}\nmore_context{i}")
        for i in range(20)
    ]
    content = render_edit_diff(_edit(*hunk_pairs, path=""))

    assert len(calls) == 1  # only the group inside the preview window was checked
    assert content is not None
    bold_text = {text for text, _ in _bold_spans(content)}
    assert bold_text == {"old", "new"}  # the one visible group is still highlighted


def test_intra_line_highlight_never_scans_a_group_entirely_past_the_preview(
    monkeypatch,
) -> None:
    # Minimal, direct version of the P2 finding: a multi-edit diff where an
    # earlier hunk's own lines push a later equal-length replace-group
    # entirely past diff_lines[:_DIFF_PREVIEW_LINES] — that later group must
    # never reach _intra_line_ranges_for_group at all, not "guarded and
    # skipped past its own ceiling", genuinely never invoked for that group.
    import wisp.tui.tool_output as mod

    calls: list[tuple] = []
    real = mod._intra_line_ranges_for_group

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(mod, "_intra_line_ranges_for_group", spy)

    # 5 independent equal-length-replace hunks; only the first is fully
    # within the 8-line preview window (see the module-level constant).
    hunks = [(f"ctx{i}\nold{i}", f"ctx{i}\nnew{i}") for i in range(5)]
    render_edit_diff(_edit(*hunks, path=""))

    assert len(calls) == 1
    assert calls[0] == (["old0"], ["new0"])


def test_intra_line_highlight_does_not_treat_a_truncated_replace_as_equal_length() -> None:
    # P2 regression guard: a genuinely UNEQUAL-length replace (3 deleted
    # lines, 4 added lines) whose "+" run is cut off exactly at the preview
    # window boundary must never be highlighted — scanning only the clipped
    # slice would see 3 "-" lines followed by 3 visible "+" lines (the 4th
    # "+" line falls outside the window) and misread it as a false 3-for-3
    # equal-length group. _equal_length_replace_groups must look past the
    # window to see the 4th "+" line still belongs to the same run and
    # exclude the whole group, not partially highlight it.
    old = "ctx\nA\nB\nC"
    new = "ctx\nA2\nB2\nC2\nD2"
    content = render_edit_diff(_edit((old, new)))

    assert content is not None
    assert _bold_spans(content) == []


def test_intra_line_highlight_still_applies_when_equal_length_group_ends_at_diff_end() -> None:
    # Companion to the truncation-false-positive guard above: a group whose
    # "+" run happens to end exactly where the whole diff ends (genuinely
    # equal-length, not cut off by the preview window — there's nothing more
    # after it in the FULL diff either) must still get highlighted. Proves
    # the look-past-the-window check distinguishes "truncated by the window"
    # from "the diff naturally ends here".
    old = "ctx\nA\nB\nC"
    new = "ctx\nA2\nB2\nC2"  # exactly 3-for-3, nothing follows
    content = render_edit_diff(_edit((old, new)))

    bold_text = {text for text, _ in _bold_spans(content)}
    assert bold_text == {"2"}


def test_intra_line_highlight_style_is_distinguishable_without_color() -> None:
    # The non-color-cue acceptance criterion, directly: the highlighted span's
    # style carries the bold modifier; the surrounding whole-line span at the
    # same line does not.
    content = render_edit_diff(_edit(("return old_value", "return new_value")))
    plain_del_spans = [
        str(span.style)
        for span in content.spans
        if str(span.style) == _DIFF_DEL_STYLE and content.plain[span.start : span.end]
    ]
    bold_del_spans = [style for _, style in _bold_spans(content) if _DIFF_DEL_STYLE in style]
    assert plain_del_spans  # unhighlighted portion of the line still present
    assert bold_del_spans  # highlighted portion carries the extra modifier
    assert plain_del_spans[0] != bold_del_spans[0]


def test_render_edit_diff_intra_line_highlight_clipped_by_preview_bounds() -> None:
    # A highlighted line truncated by the existing byte-preview clip must not
    # crash and must not produce an out-of-range span.
    padding = "x" * (_DIFF_PREVIEW_BYTES - 20)
    old = f"return old_value {padding}"
    new = f"return new_value {padding}"
    content = render_edit_diff(_edit((old, new)))
    assert isinstance(content, Content)
    for span in content.spans:
        assert 0 <= span.start <= span.end <= len(content.plain)


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
