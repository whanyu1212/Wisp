"""Unit tests for native ToolCard collapse/expand and truncation presentation.

The card is a Textual ``Collapsible``: the title is the compact scan layer and
expanded children contain full tool detail. These tests inspect that real child
structure without an app; click/focus wiring is covered by integration tests.
"""

from __future__ import annotations

from textual.content import Content

from wisp.tui.widgets import ToolCard, _summarize_arguments


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
    """Return the currently visible title/content children as plain text."""

    lines = [card._title.render().plain]
    if not card.collapsed:
        if card._detail_widget.display:
            lines.append(card._detail_widget.render().plain)
        if card._pretty_widget.display:
            lines.append(str(card._pretty_widget.render()))
        if card._truncation_widget.display:
            lines.append(card._truncation_widget.render().plain)
    return "\n".join(lines)


def test_argument_summary_stays_on_one_bounded_line() -> None:
    summary = _summarize_arguments({"path": "src/file.py", "content": "one\ntwo\n" + "three " * 20})

    assert "\n" not in summary
    assert len(summary) <= 48
    assert summary.endswith("…")


def test_collapsed_shows_summary_and_expand_affordance() -> None:
    card = _resolved(detail="short preview", full_output="line 1\nline 2\nline 3\nline 4\n")
    text = _rendered(card)

    assert "short preview" in text
    assert "line 4" not in text
    assert "▶" in text


def test_toggle_expands_to_full_output() -> None:
    full = "".join(f"line {i}\n" for i in range(20))
    card = _resolved(detail="first few lines…", full_output=full)
    assert card._expanded is False

    card.action_toggle_expand()
    text = _rendered(card)
    assert card._expanded is True
    assert "line 19" in text
    assert "▼" in text

    card.action_toggle_expand()
    assert card._expanded is False
    assert "line 19" not in _rendered(card)


def test_toggle_is_noop_without_expandable_content() -> None:
    card = _resolved(detail="all of it", full_output="all of it")

    assert card._can_expand() is False
    card.action_toggle_expand()
    assert card._expanded is False
    assert "▶" not in _rendered(card)


def test_error_card_without_full_output_cannot_expand() -> None:
    card = ToolCard("bash", {})
    card.set_state("error", detail="command failed: not found", elapsed=0.1)

    assert card._can_expand() is False
    assert "▶" not in _rendered(card)


def test_truncation_is_disclosed_in_title_and_expanded_detail() -> None:
    full = "".join(f"row {i}\n" for i in range(20))
    card = _resolved(detail="preview", full_output=full, truncated=True)
    marker = "truncated at the tool's limit"

    # The collapsed title stays honest without mounting the detail payload.
    assert "output truncated" in _rendered(card)
    assert marker not in _rendered(card)

    card.action_toggle_expand()
    assert marker in _rendered(card)
    assert _rendered(card).count(marker) == 1

    card.action_toggle_expand()
    assert "output truncated" in _rendered(card)


def test_multiline_capped_output_expands_when_preview_equals_full_output() -> None:
    # The outer layer intentionally shows a one-line summary. A multiline result
    # stays expandable even if the retained output has no additional preview clip.
    card = _resolved(detail="line 1\nline 2", full_output="line 1\nline 2", truncated=True)

    assert card._can_expand() is True
    assert "▶" in _rendered(card)
    assert "output truncated" in _rendered(card)
    assert "line 2" not in _rendered(card)

    card.action_toggle_expand()
    assert "line 2" in _rendered(card)
    assert "truncated at the tool's limit" in _rendered(card)


def test_no_truncation_marker_when_tool_returned_everything() -> None:
    full = "".join(f"row {i}\n" for i in range(20))
    card = _resolved(detail="preview", full_output=full, truncated=False)

    assert "output truncated" not in _rendered(card)
    card.action_toggle_expand()
    assert "truncated at the tool's limit" not in _rendered(card)


def test_expanded_output_stays_literal_no_markup_injection() -> None:
    full = "[red]INJECT[/red]\n" + "".join(f"line {i}\n" for i in range(20))
    card = _resolved(detail="preview", full_output=full)

    card.action_toggle_expand()
    rendered = card._detail_widget.render()
    assert "[red]INJECT[/red]" in rendered.plain
    assert all("red" not in str(span.style).lower() for span in rendered.spans)


def test_content_detail_expands_to_raw_output() -> None:
    diff = Content.styled("@@ -1 +1 @@", "dim")
    raw = "".join(f"context line {i}\n" for i in range(20))
    card = _resolved(detail=diff, full_output=raw)

    assert card._can_expand() is True
    card.action_toggle_expand()
    assert "context line 19" in _rendered(card)


def test_collapsed_header_keeps_untrusted_markup_literal() -> None:
    card = ToolCard("tool[/blue]", {"path": "[red]danger[/red]"})
    card.set_state(
        "done",
        detail="[green]result[/green]",
        elapsed=0.1,
        full_output="[green]result[/green]",
    )

    title = card._title.render()
    assert "tool[/blue]" in title.plain
    assert "[red]danger[/red]" in title.plain
    assert "[green]result[/green]" in title.plain
    assert all("red" not in str(span.style).lower() for span in title.spans)
    assert all("green" not in str(span.style).lower() for span in title.spans)
