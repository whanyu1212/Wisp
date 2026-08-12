"""Focused visual-contract tests for Rich-backed assistant Markdown."""

from __future__ import annotations

import re
from typing import cast

import anyio
import pytest
from rich.console import RenderableType
from rich.segment import Segment
from rich.style import Style as RichStyle

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


def _segments(app: TextualTui, stream: StreamMessage, *, width: int = 80) -> list[Segment]:
    options = app.console_options.update(width=width, height=None)
    return list(app.console.render(cast(RenderableType, stream.content), options))


def _plain(segments: list[Segment]) -> str:
    return "".join(segment.text for segment in segments)


def _style_for(segments: list[Segment], text: str) -> RichStyle:
    return next(
        segment.style for segment in segments if text in segment.text and segment.style is not None
    )


@pytest.mark.parametrize("theme_name", ["wisp", "wisp-light"])
def test_assistant_markdown_uses_semantic_theme_styles(theme_name: str) -> None:
    async def scenario() -> tuple[list[Segment], dict[str, str]]:
        app = TextualTui()
        async with app.run_test(size=(80, 30)) as pilot:
            app.theme = theme_name
            stream = StreamMessage()
            await app.query_one("#transcript", Transcript).mount(stream)
            await stream.replace_markdown(_DOCUMENT)
            await pilot.pause()
            theme = app.current_theme
            return _segments(app, stream), {
                "accent": theme.accent or "",
                "foreground": theme.foreground or "",
                "warning": theme.warning or "",
            }

    segments, colors = anyio.run(scenario)
    heading = _style_for(segments, "Heading")
    link = _style_for(segments, "a link")
    inline_code = _style_for(segments, "inline code")
    bullet = _style_for(segments, "•")

    assert heading.bold and heading.underline
    assert heading.color is not None
    assert heading.color.get_truecolor().hex.lower() == colors["warning"].lower()
    assert link.underline
    assert "@click" in link.meta
    assert inline_code.bold and inline_code.bgcolor is not None
    assert bullet.bold
    assert bullet.color is not None
    assert bullet.color.get_truecolor().hex.lower() == colors["accent"].lower()


def test_assistant_markdown_renders_structure_in_one_widget() -> None:
    async def scenario() -> tuple[str, str, int]:
        app = TextualTui()
        async with app.run_test(size=(80, 30)) as pilot:
            stream = StreamMessage()
            await app.query_one("#transcript", Transcript).mount(stream)
            await stream.replace_markdown(_DOCUMENT)
            await pilot.pause()
            return stream.source, _plain(_segments(app, stream)), len(stream.children)

    source, rendered, child_count = anyio.run(scenario)
    assert source == _DOCUMENT
    assert child_count == 0
    for visible in ("Heading", "a link", "inline code", "Quoted text", "first", "print"):
        assert visible in rendered
    assert "▌" in rendered
    assert "•" in rendered


def test_assistant_markdown_link_metadata_routes_through_the_app() -> None:
    async def scenario() -> tuple[str, list[str]]:
        app = TextualTui()
        opened: list[str] = []
        app.open_url = opened.append  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            stream = StreamMessage()
            await app.query_one("#transcript", Transcript).mount(stream)
            await stream.replace_markdown("A [link](https://example.com/a?q=1).")
            await pilot.pause()
            link_style = _style_for(_segments(app, stream), "link")
            stream.action_open_markdown_link("https://example.com/a?q=1")
            return str(link_style.meta["@click"]), opened

    action, opened = anyio.run(scenario)
    assert action == "open_markdown_link('https://example.com/a?q=1')"
    assert opened == ["https://example.com/a?q=1"]


@pytest.mark.parametrize("size", [(28, 20), (80, 24), (120, 40)])
def test_assistant_markdown_wraps_without_horizontal_transcript_overflow(
    size: tuple[int, int],
) -> None:
    source = (
        "## Width\n\n"
        "A paragraph with enough words to wrap at narrow widths and `inline_code_value`.\n\n"
        "> quoted material that also wraps\n\n"
        "- a list item with enough content to wrap\n\n"
        "```python\nprint('a deliberately long source line that wraps in the transcript')\n```"
    )

    async def scenario() -> tuple[str, int, int, str]:
        app = TextualTui()
        async with app.run_test(size=size) as pilot:
            stream = StreamMessage()
            transcript = app.query_one("#transcript", Transcript)
            await transcript.mount(stream)
            await stream.replace_markdown(source)
            await pilot.pause()
            return (
                stream.source,
                stream.content_size.width,
                transcript.max_scroll_x,
                _plain(_segments(app, stream, width=max(1, stream.content_size.width))),
            )

    rendered_source, width, transcript_scroll_x, rendered = anyio.run(scenario)
    assert rendered_source == source
    assert width <= size[0]
    assert transcript_scroll_x == 0
    assert "deliberately long source line" in re.sub(r"\s+", " ", rendered)


def test_assistant_markdown_theme_switch_restyles_existing_content() -> None:
    async def scenario() -> tuple[str, str, str]:
        app = TextualTui()
        async with app.run_test() as pilot:
            stream = StreamMessage("# Heading")
            await app.query_one("#transcript", Transcript).mount(stream)
            await pilot.pause()
            dark = _style_for(_segments(app, stream), "Heading")
            app.theme = "wisp-light"
            await pilot.pause()
            light = _style_for(_segments(app, stream), "Heading")
            return (
                dark.color.get_truecolor().hex if dark.color is not None else "",
                light.color.get_truecolor().hex if light.color is not None else "",
                stream.source,
            )

    dark, light, source = anyio.run(scenario)
    assert dark != light
    assert source == "# Heading"


def test_assistant_markdown_keeps_non_color_cues(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")

    async def scenario() -> str:
        app = TextualTui()
        async with app.run_test() as pilot:
            stream = StreamMessage(_DOCUMENT)
            await app.query_one("#transcript", Transcript).mount(stream)
            await pilot.pause()
            return _plain(_segments(app, stream))

    rendered = anyio.run(scenario)
    assert "Heading" in rendered
    assert "•" in rendered
    assert "▌" in rendered
    assert 'print("hello")' in rendered


@pytest.mark.parametrize("native_ansi", [False, True])
def test_assistant_ansi_fence_never_emits_raw_escape_sequences(native_ansi: bool) -> None:
    async def scenario() -> tuple[str, RichStyle | None]:
        app = TextualTui()
        app.ansi_color = native_ansi
        async with app.run_test() as pilot:
            stream = StreamMessage("```ansi\n\x1b[31mred\x1b[0m\n```")
            await app.query_one("#transcript", Transcript).mount(stream)
            await pilot.pause()
            segments = _segments(app, stream)
            style = next(
                (segment.style for segment in segments if "red" in segment.text),
                None,
            )
            return _plain(segments), style

    rendered, style = anyio.run(scenario)
    assert rendered.strip() == "red"
    assert "\x1b" not in rendered
    if native_ansi:
        assert style is not None
        assert style.color is not None
    else:
        assert style is None or style.color is None


def test_assistant_markdown_treats_rich_markup_and_controls_as_text() -> None:
    source = "[red]not markup[/red] and an escape: \x1b[31mred\x1b[0m"

    async def scenario() -> tuple[str, str]:
        app = TextualTui()
        async with app.run_test() as pilot:
            stream = StreamMessage(source)
            await app.query_one("#transcript", Transcript).mount(stream)
            await pilot.pause()
            return stream.source, _plain(_segments(app, stream))

    retained, rendered = anyio.run(scenario)
    assert retained == source
    assert "[red]not markup[/red]" in rendered
    assert "\x1b" not in rendered
