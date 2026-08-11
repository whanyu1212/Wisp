"""Focused visual-contract tests for streamed assistant Markdown."""

from __future__ import annotations

from typing import cast

import anyio
import pytest
from textual.content import Content
from textual.style import Style
from textual.widget import Widget
from textual.widgets import Static
from textual.widgets.markdown import MarkdownFence

from wisp.tui.textual_app import TextualTui
from wisp.tui.widgets import StreamMessage, Transcript

pytestmark = pytest.mark.tui

_DOCUMENT = """# Heading

Paragraph with [a link](https://example.com) and `inline code`.

> Quoted text

- first
- second

---

```python
print("hello")
```
"""


def _widget_named(root: Widget, name: str) -> Widget:
    return next(widget for widget in root.walk_children() if type(widget).__name__ == name)


def _resolved_hex(value: str) -> str:
    return value.lower()


@pytest.mark.parametrize("theme", ["wisp", "wisp-light"])
def test_assistant_markdown_uses_semantic_theme_styles(theme: str) -> None:
    async def scenario() -> tuple[dict[str, object], dict[str, str]]:
        app = TextualTui()
        async with app.run_test(size=(80, 30)) as pilot:
            app.theme = theme
            stream = StreamMessage()
            await app.query_one("#transcript", Transcript).mount(stream)
            await stream.replace_markdown(_DOCUMENT)
            await pilot.pause()

            markdown = stream._markdown
            heading = _widget_named(markdown, "MarkdownH1")
            paragraph = _widget_named(markdown, "MarkdownParagraph")
            quote = _widget_named(markdown, "MarkdownBlockQuote")
            bullet = _widget_named(markdown, "MarkdownBullet")
            rule = _widget_named(markdown, "MarkdownHorizontalRule")
            fence = markdown.query_one(MarkdownFence)
            inline_code = paragraph.get_component_rich_style("code_inline")
            variables = app.get_css_variables()
            return (
                {
                    "markdown_padding": markdown.styles.padding,
                    "heading_color": heading.styles.color.hex.lower(),
                    "heading_style": heading.rich_style,
                    "heading_align": heading.styles.content_align,
                    "link_color": paragraph.styles.link_color.hex.lower(),
                    "link_hover": paragraph.styles.link_color_hover.hex.lower(),
                    "link_style": paragraph.styles.link_style,
                    "link_hover_style": paragraph.styles.link_style_hover,
                    "inline_code": str(inline_code).lower(),
                    "quote_color_alpha": quote.styles.color.a,
                    "quote_background_alpha": quote.styles.background.a,
                    "quote_border": quote.styles.border_left,
                    "bullet_color": bullet.styles.color.hex.lower(),
                    "rule_border": rule.styles.border_bottom,
                    "fence_background": fence.styles.background.hex.lower(),
                    "fence_border": fence.styles.border_left,
                    "fence_overflow": fence.styles.overflow_x,
                    "fence_scrollbar": fence.styles.scrollbar_size_horizontal,
                },
                {
                    name: variables[name].lower()
                    for name in (
                        "accent",
                        "markdown-h1-color",
                        "panel",
                        "secondary",
                        "text-accent",
                        "text-primary",
                    )
                },
            )

    styles, variables = anyio.run(scenario)
    padding = styles["markdown_padding"]
    assert padding.top == padding.right == padding.bottom == padding.left == 0
    assert styles["heading_color"] == _resolved_hex(variables["markdown-h1-color"])
    assert styles["heading_style"].bold
    assert styles["heading_style"].underline
    assert styles["heading_align"] == ("left", "middle")
    assert styles["link_color"] == _resolved_hex(variables["text-primary"])
    assert styles["link_hover"] == _resolved_hex(variables["text-accent"])
    assert styles["link_style"].underline
    assert styles["link_hover_style"].bold
    assert styles["link_hover_style"].underline
    assert variables["text-accent"] in styles["inline_code"]
    assert variables["panel"] in styles["inline_code"]
    assert styles["quote_color_alpha"] == pytest.approx(0.6)
    assert styles["quote_background_alpha"] == 0
    assert styles["quote_border"][0] == "outer"
    assert styles["quote_border"][1].hex.lower() == variables["secondary"]
    assert styles["bullet_color"] == variables["accent"]
    assert styles["rule_border"][1].hex.lower() == variables["secondary"]
    assert styles["fence_background"] == variables["panel"]
    assert styles["fence_border"][0] == "outer"
    assert styles["fence_border"][1].hex.lower() == variables["secondary"]
    assert styles["fence_overflow"] == "scroll"
    assert styles["fence_scrollbar"] == 0


def test_wisp_themes_define_all_markdown_heading_hooks() -> None:
    from wisp.tui.theme import WISP_THEME_DARK, WISP_THEME_LIGHT

    for theme in (WISP_THEME_DARK, WISP_THEME_LIGHT):
        for level in range(1, 7):
            assert theme.variables[f"markdown-h{level}-color"] == theme.warning
            expected_style = "bold underline" if level == 1 else "bold"
            assert theme.variables[f"markdown-h{level}-text-style"] == expected_style


def test_assistant_markdown_link_styling_preserves_click_metadata() -> None:
    async def scenario() -> tuple[list[Style], Style, Style]:
        app = TextualTui()
        async with app.run_test() as pilot:
            stream = StreamMessage()
            await app.query_one("#transcript", Transcript).mount(stream)
            await stream.replace_markdown("A [link](https://example.com).")
            await pilot.pause()

            paragraph = cast(Static, _widget_named(stream._markdown, "MarkdownParagraph"))
            content = cast(Content, paragraph.render())
            metadata_styles = [
                span.style
                for span in content.spans
                if isinstance(span.style, Style) and "@click" in span.style.meta
            ]
            return metadata_styles, paragraph.styles.link_style, paragraph.styles.link_style_hover

    metadata_styles, link_style, hover_style = anyio.run(scenario)
    assert len(metadata_styles) == 1
    assert metadata_styles[0].meta["@click"] == "link('https://example.com')"
    assert link_style.underline
    assert hover_style.bold and hover_style.underline


def test_assistant_markdown_spacing_tracks_first_and_last_blocks() -> None:
    async def scenario() -> tuple[int, int, int, int, int, int, int, int]:
        app = TextualTui()
        async with app.run_test() as pilot:
            transcript = app.query_one("#transcript", Transcript)

            prose = StreamMessage()
            await transcript.mount(prose)
            await prose.replace_markdown("# Heading\n\nFirst paragraph.\n\nLast paragraph.")

            quote = StreamMessage()
            await transcript.mount(quote)
            await quote.replace_markdown("> First quote paragraph.\n>\n> Last quote paragraph.")

            fence = StreamMessage()
            await transcript.mount(fence)
            await fence.replace_markdown("```text\ncode\n```")

            listing = StreamMessage()
            await transcript.mount(listing)
            await listing.replace_markdown("- one\n- two")
            await pilot.pause()

            heading = _widget_named(prose._markdown, "MarkdownH1")
            paragraphs = [
                widget
                for widget in prose._markdown.walk_children()
                if type(widget).__name__ == "MarkdownParagraph"
            ]
            quote_block = _widget_named(quote._markdown, "MarkdownBlockQuote")
            quote_paragraphs = [
                widget
                for widget in quote_block.walk_children()
                if type(widget).__name__ == "MarkdownParagraph"
            ]
            fence_block = fence._markdown.query_one(MarkdownFence)
            list_block = _widget_named(listing._markdown, "MarkdownBulletList")
            return (
                heading.styles.margin.top,
                paragraphs[0].styles.margin.bottom,
                paragraphs[-1].styles.margin.bottom,
                quote_block.styles.margin.top,
                quote_block.styles.margin.bottom,
                quote_paragraphs[0].styles.margin.bottom,
                quote_paragraphs[-1].styles.margin.bottom,
                max(fence_block.styles.margin.bottom, list_block.styles.margin.bottom),
            )

    assert anyio.run(scenario) == (0, 1, 0, 0, 0, 1, 0, 0)


@pytest.mark.parametrize("size", [(28, 20), (80, 24), (120, 40)])
def test_assistant_markdown_stays_bounded_at_supported_widths(size: tuple[int, int]) -> None:
    source = (
        "## Width\n\n"
        "A paragraph with enough words to wrap at narrow widths and `inline_code_value`.\n\n"
        "> quoted material that also wraps\n\n"
        "- a list item with enough content to wrap\n\n"
        "```python\nprint('a deliberately long source line that scrolls horizontally')\n```"
    )

    async def scenario() -> tuple[str, int, int, list[int], str, int]:
        app = TextualTui()
        async with app.run_test(size=size) as pilot:
            stream = StreamMessage()
            transcript = app.query_one("#transcript", Transcript)
            await transcript.mount(stream)
            await stream.replace_markdown(source)
            await pilot.pause()

            markdown = stream._markdown
            fence = markdown.query_one(MarkdownFence)
            return (
                markdown.source,
                markdown.content_size.width,
                transcript.max_scroll_x,
                [child.outer_size.width for child in markdown.children],
                fence.styles.overflow_x,
                fence.styles.scrollbar_size_horizontal,
            )

    rendered, markdown_width, transcript_scroll_x, block_widths, overflow, scrollbar = anyio.run(
        scenario
    )
    assert rendered == source
    assert transcript_scroll_x == 0
    assert all(width <= markdown_width for width in block_widths)
    assert overflow == "scroll"
    assert scrollbar == 0


def test_assistant_markdown_theme_switch_restyles_existing_blocks() -> None:
    async def scenario() -> tuple[tuple[str, str, str], tuple[str, str, str], dict[str, str]]:
        app = TextualTui()
        async with app.run_test() as pilot:
            stream = StreamMessage()
            await app.query_one("#transcript", Transcript).mount(stream)
            await stream.replace_markdown("# Heading\n\nA [link](https://example.com).\n\n- item")
            await pilot.pause()

            heading = _widget_named(stream._markdown, "MarkdownH1")
            paragraph = _widget_named(stream._markdown, "MarkdownParagraph")
            bullet = _widget_named(stream._markdown, "MarkdownBullet")
            dark = (
                heading.styles.color.hex.lower(),
                paragraph.styles.link_color.hex.lower(),
                bullet.styles.color.hex.lower(),
            )

            app.theme = "wisp-light"
            await pilot.pause()
            variables = app.get_css_variables()
            light = (
                heading.styles.color.hex.lower(),
                paragraph.styles.link_color.hex.lower(),
                bullet.styles.color.hex.lower(),
            )
            return (
                dark,
                light,
                {
                    key: variables[key].lower()
                    for key in ("markdown-h1-color", "text-primary", "accent")
                },
            )

    dark, light, variables = anyio.run(scenario)
    assert dark != light
    assert light == (
        variables["markdown-h1-color"],
        variables["text-primary"],
        variables["accent"],
    )


def test_assistant_markdown_keeps_non_color_cues(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")

    async def scenario() -> tuple[Style, Style, str, str, str, str]:
        app = TextualTui()
        async with app.run_test() as pilot:
            stream = StreamMessage()
            await app.query_one("#transcript", Transcript).mount(stream)
            await stream.replace_markdown(_DOCUMENT)
            await pilot.pause()

            heading = _widget_named(stream._markdown, "MarkdownH1")
            paragraph = _widget_named(stream._markdown, "MarkdownParagraph")
            quote = _widget_named(stream._markdown, "MarkdownBlockQuote")
            bullet = cast(Static, _widget_named(stream._markdown, "MarkdownBullet"))
            fence = stream._markdown.query_one(MarkdownFence)
            return (
                heading.rich_style,
                paragraph.styles.link_style,
                cast(Content, bullet.render()).plain,
                quote.styles.border_left[0],
                fence.styles.border_left[0],
                stream._markdown.source,
            )

    heading_style, link_style, bullet, quote_rail, fence_rail, source = anyio.run(scenario)
    assert heading_style.bold and heading_style.underline
    assert link_style.underline
    assert bullet == "• "
    assert quote_rail == fence_rail == "outer"
    assert source == _DOCUMENT


def test_assistant_ansi_fence_keeps_transparent_compact_presentation() -> None:
    async def scenario() -> tuple[int, str, str, int, int]:
        app = TextualTui()
        app.ansi_color = True
        async with app.run_test() as pilot:
            stream = StreamMessage()
            await app.query_one("#transcript", Transcript).mount(stream)
            await stream.replace_markdown("```ansi\n\\x1b[31mred\\x1b[0m\n```")
            await pilot.pause()

            fence = stream._markdown.query_one(MarkdownFence)
            label = fence.query_one("#code-content", Static)
            return (
                fence.styles.background.a,
                fence.styles.border_left[0],
                fence.styles.overflow_x,
                label.styles.padding.top,
                label.styles.padding.left,
            )

    assert anyio.run(scenario) == (0, "", "scroll", 1, 0)
