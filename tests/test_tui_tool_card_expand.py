"""Unit tests for ToolCard expand/collapse + truncation marker (issue #74 PR D).

These exercise the card's render logic directly (no running app): a resolved card
retains its full output and swaps between the collapsed detail and the full output
on toggle, offers the toggle only when expanding adds something, and marks output the
tool itself truncated. Focus/keybinding wiring is covered by the integration tests.
"""

from __future__ import annotations

from textual.content import Content

from wisp.tui.widgets import ToolCard


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


def test_collapsed_shows_detail_and_expand_affordance() -> None:
    card = _resolved(detail="short preview", full_output="line 1\nline 2\nline 3\nline 4\n")
    text = _rendered(card)
    assert "short preview" in text
    assert "line 4" not in text  # full output hidden while collapsed
    assert "▸" in text  # collapsed affordance


def test_toggle_expands_to_full_output() -> None:
    full = "".join(f"line {i}\n" for i in range(20))
    card = _resolved(detail="first few lines…", full_output=full)
    assert card._expanded is False

    card.action_toggle_expand()
    text = _rendered(card)
    assert card._expanded is True
    assert "line 19" in text  # the full output is now visible
    assert "▾" in text  # expanded affordance

    card.action_toggle_expand()
    assert card._expanded is False
    assert "line 19" not in _rendered(card)  # collapsed again


def test_toggle_is_noop_without_expandable_content() -> None:
    # A short output whose preview already IS the whole output has nothing to expand.
    card = _resolved(detail="all of it", full_output="all of it")
    assert card._can_expand() is False
    card.action_toggle_expand()
    assert card._expanded is False
    assert "▸" not in _rendered(card)  # no affordance offered


def test_error_card_without_full_output_cannot_expand() -> None:
    card = ToolCard("bash", {})
    card.set_state("error", detail="command failed: not found", elapsed=0.1)
    assert card._can_expand() is False
    assert "▸" not in _rendered(card)


def test_truncation_marker_only_when_expanded_and_truncated() -> None:
    full = "".join(f"row {i}\n" for i in range(20))
    card = _resolved(detail="preview", full_output=full, truncated=True)
    marker = "truncated at the tool's limit"

    assert marker not in _rendered(card)  # collapsed: no marker
    card.action_toggle_expand()
    assert marker in _rendered(card)  # expanded + truncated: honest marker
    card.action_toggle_expand()
    assert marker not in _rendered(card)  # collapsed again


def test_no_truncation_marker_when_tool_returned_everything() -> None:
    full = "".join(f"row {i}\n" for i in range(20))
    card = _resolved(detail="preview", full_output=full, truncated=False)
    card.action_toggle_expand()
    assert "truncated at the tool's limit" not in _rendered(card)


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
