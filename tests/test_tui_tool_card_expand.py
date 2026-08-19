"""Unit tests for ToolCard expand/collapse + truncation marker (issue #74 PR D).

These exercise the card's render logic directly (no running app): a resolved card
retains its full output and swaps between the collapsed detail and the full output
on toggle, offers the toggle only when expanding adds something, and marks output the
tool itself truncated. Focus/keybinding wiring is covered by the integration tests.
"""

from __future__ import annotations

import gc
import time
import weakref

from textual.content import Content

from wisp.tui.widgets import ToolCard, _tree_detail, _tree_line


def _resolved(
    *,
    detail: str | Content = "preview",
    full_output: str = "",
    truncated: bool = False,
) -> ToolCard:
    card = ToolCard("read", {"path": "foo.py"})
    card.set_state(
        "done",
        detail=detail,
        elapsed=0.1,
        full_output=full_output,
        truncated=truncated,
    )
    return card


def _rendered(card: ToolCard) -> str:
    return card.render().plain


def _span_style(card: ToolCard, text: str) -> str:
    content = card.render()
    return next(
        str(span.style) for span in content.spans if text in content.plain[span.start : span.end]
    )


def test_pending_card_has_only_a_flat_action_row() -> None:
    card = ToolCard("bash", {"command": "pytest -q"})

    assert _rendered(card) == "• Running  pytest -q"


def test_multiline_result_uses_one_branch_and_aligned_continuations() -> None:
    card = ToolCard("bash", {"command": "pytest -q"})
    card.set_state("done", detail="first line\nsecond line", elapsed=1.2)

    assert _rendered(card) == "• Ran  pytest -q · 1.2s\n  └ first line\n    second line"
    assert _span_style(card, "Ran") == "bold $success"
    assert _span_style(card, "pytest") == "$accent"
    assert _span_style(card, "1.2s") == "$text-muted"


def test_fitting_styled_action_preserves_separator_before_arguments() -> None:
    card = ToolCard("write", {"path": "src/example.py"})
    card.set_state("done", detail="written")

    assert _rendered(card).startswith("• Wrote  src/example.py")
    assert "Wrotesrc" not in _rendered(card)


def test_card_does_not_retain_raw_write_payload_after_bounding_arguments() -> None:
    class Payload:
        pass

    payload = Payload()
    retained = weakref.ref(payload)
    arguments = {"path": "src/example.py", "content": payload}

    card = ToolCard("write", arguments)
    del arguments, payload
    gc.collect()

    assert retained() is None
    card.set_state("done", detail="written")
    assert _rendered(card).startswith("• Wrote  src/example.py")


def test_tree_helpers_use_hanging_indents_when_wrapping() -> None:
    parent = _tree_line(
        Content("Ran abcdefghij"),
        width=10,
        first_prefix="• ",
        continuation_prefix="  ",
    )
    detail = _tree_detail("abcdefghijkl", width=10)

    assert parent.plain == "• Ran\n  abcdefgh\n  ij"
    assert detail.plain == "  └ abcdef\n    ghijkl"


def test_collapsed_shows_detail_and_expand_affordance() -> None:
    card = _resolved(detail="short preview", full_output="line 1\nline 2\nline 3\nline 4\n")
    text = _rendered(card)
    assert "short preview" in text
    assert "line 4" not in text  # full output hidden while collapsed
    assert "▸ more (Enter)" in text  # labeled collapsed affordance


def test_toggle_expands_to_full_output() -> None:
    full = "".join(f"line {i}\n" for i in range(20))
    card = _resolved(detail="first few lines…", full_output=full)
    assert card._expanded is False

    card.action_toggle_expand()
    text = _rendered(card)
    assert card._expanded is True
    assert "line 19" in text  # the full output is now visible
    assert "▾ less (Enter)" in text  # labeled expanded affordance

    card.action_toggle_expand()
    assert card._expanded is False
    assert "line 19" not in _rendered(card)  # collapsed again


def test_resolved_card_keeps_semantic_call_arguments_in_header() -> None:
    card = ToolCard(
        "grep",
        {"pattern": "TODO", "path": "src", "glob": "*.py"},
    )
    card.set_state(
        "done",
        detail="grep: 2 matches",
        elapsed=0.1,
        full_output="grep: 2 matches\nsrc/a.py:1:TODO\nsrc/b.py:2:TODO",
    )

    rendered = _rendered(card)
    assert "• Searched  /TODO/ in src (*.py) · 0.1s" in rendered
    assert "  └ grep: 2 matches" in rendered


def test_toggle_is_noop_without_expandable_content() -> None:
    # A short output whose preview already IS the whole output has nothing to expand.
    card = _resolved(detail="all of it", full_output="all of it")
    assert card._can_expand() is False
    card.action_toggle_expand()
    assert card._expanded is False
    assert "more (Enter)" not in _rendered(card)  # no affordance offered


def test_error_card_without_full_output_cannot_expand() -> None:
    card = ToolCard("bash", {})
    card.set_state("error", detail="command failed: not found", elapsed=0.1)
    assert card._can_expand() is False
    assert "more (Enter)" not in _rendered(card)


def test_truncation_marker_shows_when_truncated_collapsed_and_expanded() -> None:
    # Truncation is a property of the output, not of the expanded view: the honest
    # "tool capped this" marker must show whether the card is collapsed or expanded,
    # so a reader who never expands still knows the output is incomplete.
    full = "".join(f"row {i}\n" for i in range(20))
    card = _resolved(detail="preview", full_output=full, truncated=True)
    marker = "truncated at the tool's limit"

    assert marker in _rendered(card)  # collapsed: marker present
    assert _span_style(card, marker) == "$warning"
    card.action_toggle_expand()
    assert marker in _rendered(card)  # expanded: still present, not duplicated
    assert _rendered(card).count(marker) == 1
    card.action_toggle_expand()
    assert marker in _rendered(card)  # collapsed again: still present


def test_truncation_marker_shows_when_capped_output_fits_and_cannot_expand() -> None:
    # A tool that capped its output but returned a buffer already equal to the
    # collapsed detail has nothing extra to expand (_can_expand is False). The
    # truncation marker must still show — otherwise the capped output would present
    # as complete. This is the case Codex flagged (small max_output budgets).
    card = _resolved(detail="line 1\nline 2", full_output="line 1\nline 2", truncated=True)
    assert card._can_expand() is False  # nothing more to reveal
    rendered = _rendered(card)
    assert "more (Enter)" not in rendered  # no expand affordance offered
    assert "truncated at the tool's limit" in rendered  # but the marker is still honest


def test_no_truncation_marker_when_tool_returned_everything() -> None:
    full = "".join(f"row {i}\n" for i in range(20))
    card = _resolved(detail="preview", full_output=full, truncated=False)
    assert "truncated at the tool's limit" not in _rendered(card)  # collapsed
    card.action_toggle_expand()
    assert "truncated at the tool's limit" not in _rendered(card)  # expanded


def test_tool_card_disables_markup_parsing_like_every_other_content_static() -> None:
    # Defense-in-depth: every other content-bearing Static in widgets.py that
    # renders untrusted tool/model text passes markup=False. _repaint() always
    # wraps its content through Content(...) (never .from_markup), so this is
    # currently inert -- but it must stay false so a future call site that
    # updates the widget with a raw str directly can't reopen markup
    # injection the way the rest of the module is guarded against.
    card = ToolCard("read", {"path": "foo.py"})
    assert card._render_markup is False


def test_expanded_output_stays_literal_no_markup_injection() -> None:
    # Full output containing color-span markup must render literally when expanded,
    # never parsed — the same out-of-band styling guarantee as the collapsed views.
    full = "[red]INJECT[/red]\n" + "".join(f"line {i}\n" for i in range(20))
    card = _resolved(detail="preview", full_output=full)
    card.action_toggle_expand()
    rendered = card.render()
    assert "[red]INJECT[/red]" in rendered.plain
    # The literal brackets are text, not a parsed style span.
    assert all("red" not in str(span.style).lower() for span in rendered.spans)


def test_diff_detail_expands_to_raw_output() -> None:
    # A card whose collapsed detail is a styled diff (Content) still expands to the
    # raw output — restoring what the diff replaced.
    diff = Content.styled("@@ -1 +1 @@", "dim")
    raw = "".join(f"context line {i}\n" for i in range(20))
    card = _resolved(detail=diff, full_output=raw)
    assert card._can_expand() is True
    card.action_toggle_expand()
    assert "context line 19" in _rendered(card)


def test_tree_detail_lays_out_long_process_output_in_linear_time() -> None:
    """Expanding a long process dump must not fold its lines quadratically.

    ``Content.__add__`` copies the accumulated text and every span, so building a
    card body one line at a time was O(n^2): a few hundred lines of pytest
    progress output took seconds to lay out and stalled the whole TUI. Compare
    growth across sizes rather than absolute time so the guard stays stable.
    """

    def layout_duration(line_count: int) -> float:
        text = "\n".join("." * 72 + f" [{index % 100:3d}%]" for index in range(line_count))
        samples = []
        for _ in range(5):
            start = time.perf_counter()
            _tree_detail(text, width=110)
            samples.append(time.perf_counter() - start)
        return min(samples)

    layout_duration(50)  # warm caches
    baseline = layout_duration(125)
    quadrupled = layout_duration(500)

    # Linear predicts ~4x. The quadratic version grew ~16x (measured ~30x at
    # these sizes), so a generous 8x threshold separates them without flaking.
    assert quadrupled < baseline * 8


def test_tree_detail_preserves_line_structure_and_prefixes() -> None:
    """The linear layout must render exactly what the folded version did."""

    rendered = _tree_detail("first\nsecond\n\nfourth", width=40)

    assert rendered.plain.split("\n") == [
        "  └ first",
        "    second",
        "    ",
        "    fourth",
    ]
