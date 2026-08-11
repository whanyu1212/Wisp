"""Unit tests for the write-diff renderer (issue #74 PR B2).

These exercise `render_write_diff` and the `write` dispatch branch of
`render_tool_result` in isolation. Unlike the edit tool, the write tool's "before"
text does not live in its arguments (the args carry only the new `content`); it is
captured by the tool before the overwrite and arrives on the result event as
`before_text`. The renderer returns a Textual `Content` whose file text is literal
(never parsed as markup) and whose add/delete lines carry theme-styled spans.
"""

from __future__ import annotations

from textual.content import Content

from wisp.tui.diff_presentation import DiffOperation, DiffPresentation, DiffRowKind
from wisp.tui.tool_output import (
    _DIFF_ADD_STYLE,
    _DIFF_ADD_TOKEN_STYLE,
    _DIFF_DEL_STYLE,
    _DIFF_DEL_TOKEN_STYLE,
    _DIFF_MAX_HUNK_CHARS,
    _DIFF_META_STYLE,
    render_tool_result,
    render_write_diff,
)


def _write(content: str, *, path: str = "src/foo.py") -> dict[str, object]:
    return {"path": path, "content": content}


def _styles_at(content: Content, needle: str) -> set[str]:
    """Styles covering the first occurrence of ``needle`` in the content."""

    start = content.plain.index(needle)
    styles: set[str] = set()
    for span in content.spans:
        if span.start <= start < span.end:
            styles.add(str(span.style))
    return styles


# --- render_write_diff: overwrite -------------------------------------------


def test_render_write_diff_overwrite_shows_add_and_delete() -> None:
    content = render_write_diff("old line\n", _write("new line\n"))
    assert isinstance(content, Content)
    assert "-old line" in content.plain
    assert "+new line" in content.plain


def test_render_write_diff_colors_additions_and_deletions() -> None:
    content = render_write_diff("keep\ndrop\n", _write("keep\nadd\n"))
    assert isinstance(content, Content)
    assert _DIFF_ADD_STYLE in _styles_at(content, "+add")
    assert _DIFF_DEL_STYLE in _styles_at(content, "-drop")


def test_render_write_diff_leads_with_path_header() -> None:
    content = render_write_diff("a\n", _write("b\n", path="pkg/mod.py"))
    assert isinstance(content, Content)
    assert content.plain.startswith("pkg/mod.py\n")
    assert _DIFF_META_STYLE in _styles_at(content, "pkg/mod.py")


def test_render_write_diff_equal_length_replace_highlights_changed_token() -> None:
    # Confirmatory only (issue #111): render_write_diff shares
    # _render_diff_content with render_edit_diff, which already has the full
    # test coverage for this feature — this proves the write path benefits
    # too, without re-deriving every edge case.
    content = render_write_diff("return old_value\n", _write("return new_value\n"))
    assert isinstance(content, Content)
    token_styles = {_DIFF_ADD_TOKEN_STYLE, _DIFF_DEL_TOKEN_STYLE}
    highlighted_text = {
        content.plain[span.start : span.end]
        for span in content.spans
        if str(span.style) in token_styles
    }
    assert "old" in highlighted_text
    assert "new" in highlighted_text


# --- render_write_diff: create (no prior content) ---------------------------


def test_render_write_diff_create_is_pure_addition() -> None:
    # A newly created file has no "before" (before_text is None) but created=True;
    # render its whole content as additions so the transcript previews what was
    # written.
    content = render_write_diff(None, _write("line1\nline2\n"), created=True)
    assert isinstance(content, Content)
    assert "+line1" in content.plain
    assert "+line2" in content.plain
    # Pure add: the hunk header starts from nothing and no body line is a deletion.
    assert "@@ -0,0" in content.plain
    body = [line for line in content.plain.splitlines() if not line.startswith("@@")]
    assert not any(line.startswith("-") for line in body)


def test_render_write_diff_create_colors_additions() -> None:
    content = render_write_diff(None, _write("hello\n"), created=True)
    assert isinstance(content, Content)
    assert _DIFF_ADD_STYLE in _styles_at(content, "+hello")


def test_render_write_diff_overwrite_without_snapshot_falls_back() -> None:
    # An overwrite whose prior text couldn't be captured (binary/oversize/unreadable)
    # arrives as before_text=None with created=False. It must NOT render as a
    # pure-addition create — that would hide that an existing file was replaced. Fall
    # back to the generic summary instead.
    assert render_write_diff(None, _write("new content\n"), created=False) is None
    # Default (no created kwarg) is the conservative overwrite reading.
    assert render_write_diff(None, _write("new content\n")) is None


# --- render_write_diff: fallback cases --------------------------------------


def test_render_write_diff_noop_returns_none() -> None:
    # Rewriting a file with byte-identical content is a no-op: fall back to the
    # generic "Wrote N bytes" summary rather than an empty diff.
    assert render_write_diff("same\n", _write("same\n")) is None


def test_render_write_diff_create_empty_content_returns_none() -> None:
    # Creating an empty file (created, no before, empty after) has nothing to diff.
    assert render_write_diff(None, _write(""), created=True) is None


def test_render_write_diff_missing_content_returns_none() -> None:
    # No `content` key, or a non-string content, is malformed → generic fallback.
    assert render_write_diff("a\n", {"path": "x"}) is None
    assert render_write_diff("a\n", {"path": "x", "content": 123}) is None


def test_render_write_diff_over_ceiling_returns_none() -> None:
    # A single enormous line exceeds the work guard's per-hunk char ceiling; the
    # renderer must fall back rather than diff a multi-MB string on the event loop.
    big = "x" * (_DIFF_MAX_HUNK_CHARS + 1)
    assert render_write_diff(big, _write(big + "y")) is None


# --- render_write_diff: safety ----------------------------------------------


def test_render_write_diff_treats_markup_as_literal() -> None:
    # File content containing color-span markup must render as literal text, not as
    # an injected/broken span. Content.styled keeps it out of the markup parser.
    injected = "[red]INJECT[/red]\n"
    content = render_write_diff("before\n", _write(injected))
    assert isinstance(content, Content)
    assert "[red]INJECT[/red]" in content.plain
    # The literal brackets are text, not a style: only the diff's own add style
    # covers the line, and there is exactly one styled add span for it.
    assert _DIFF_ADD_STYLE in _styles_at(content, "+[red]INJECT")


def test_render_write_diff_stateful_str_before_is_coerced() -> None:
    # A str subclass whose __eq__/__str__ flip-flops must not desync the no-op check
    # from the diff. Coercion to a built-in str at the boundary defeats it.
    class FlipStr(str):
        _flipped = False

        def __eq__(self, other: object) -> bool:
            type(self)._flipped = not type(self)._flipped
            return type(self)._flipped

        def __hash__(self) -> int:
            return 0

        def __str__(self) -> str:
            return self

    content = render_write_diff(FlipStr("a\n"), _write("b\n"))
    assert isinstance(content, Content)
    assert "-a" in content.plain and "+b" in content.plain


# --- Dispatch: render_tool_result routes write successes here ----------------


def test_render_tool_result_routes_successful_write_to_diff() -> None:
    presentation = render_tool_result(
        "write",
        _write("after\n"),
        "Wrote 6 bytes to src/foo.py",
        is_error=False,
        exit_code=None,
        before_text="before\n",
    )
    assert isinstance(presentation, DiffPresentation)
    assert presentation.operation is DiffOperation.modify
    assert [row.kind for row in presentation.rows] == [
        DiffRowKind.hunk,
        DiffRowKind.deletion,
        DiffRowKind.addition,
    ]


def test_render_tool_result_routes_write_create_to_diff() -> None:
    # A create (before_text=None, created=True) routes to a pure-add diff from the
    # content.
    presentation = render_tool_result(
        "write",
        _write("fresh\n"),
        "Wrote 6 bytes to src/foo.py",
        is_error=False,
        exit_code=None,
        before_text=None,
        created=True,
    )
    assert isinstance(presentation, DiffPresentation)
    assert presentation.operation is DiffOperation.create
    assert presentation.additions == 1
    assert presentation.deletions == 0
    assert [row.kind for row in presentation.rows] == [DiffRowKind.hunk, DiffRowKind.addition]


def test_render_tool_result_write_overwrite_without_snapshot_falls_back() -> None:
    # An overwrite with no usable snapshot (before_text=None, created=False) must
    # fall back to the plain summary, not a create-style pure-addition diff.
    result = render_tool_result(
        "write",
        _write("fresh\n"),
        "Wrote 6 bytes to src/foo.py",
        is_error=False,
        exit_code=None,
        before_text=None,
        created=False,
    )
    assert isinstance(result, str)
    assert "Wrote 6 bytes" in result


def test_render_tool_result_write_failure_uses_error_not_diff() -> None:
    result = render_tool_result(
        "write",
        _write("after\n"),
        "write failed: permission denied",
        is_error=True,
        exit_code=None,
        before_text="before\n",
    )
    assert isinstance(result, str)
    assert "permission denied" in result


def test_render_tool_result_write_noop_falls_back_to_generic() -> None:
    # A byte-identical rewrite yields no diff → generic string summary, not a crash.
    result = render_tool_result(
        "write",
        _write("same\n"),
        "Wrote 5 bytes to src/foo.py",
        is_error=False,
        exit_code=None,
        before_text="same\n",
    )
    assert isinstance(result, str)
    assert "Wrote 5 bytes" in result
