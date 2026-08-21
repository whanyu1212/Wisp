"""Focused visual-contract tests for Rich-backed assistant Markdown."""

from __future__ import annotations

import gc
import re
import weakref
from typing import cast
from unittest.mock import patch

import anyio
import pytest
from rich.console import (
    Console,
    ConsoleOptions,
    RenderableType,
    RenderResult,
)
from rich.segment import Segment
from rich.style import Style as RichStyle
from rich.syntax import Syntax
from textual import events
from textual.visual import RenderOptions

from wisp.tui.textual_app import TextualTui
from wisp.tui.widgets import (
    StreamMessage,
    Transcript,
    _SafeAssistantMarkdown,
    _SelectableMarkdownVisual,
)

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


def _rendered_style_for(stream: StreamMessage, text: str, *, y: int = 0) -> RichStyle:
    return next(
        segment.style
        for segment in stream.render_line(y)
        if text in segment.text and segment.style is not None
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


def test_assistant_markdown_reuses_measured_strips_for_paint() -> None:
    async def scenario() -> tuple[int, int, int]:
        app = TextualTui()
        async with app.run_test(size=(80, 30)):
            stream = StreamMessage()
            await app.query_one("#transcript", Transcript).mount(stream)
            await stream.replace_markdown(_DOCUMENT)
            visual = stream._selection_visual
            assert isinstance(visual, _SelectableMarkdownVisual)
            render_count = 0
            original_render = _SafeAssistantMarkdown.__rich_console__

            def count_render(
                markdown: _SafeAssistantMarkdown,
                console: Console,
                options: ConsoleOptions,
            ) -> RenderResult:
                nonlocal render_count
                render_count += 1
                yield from original_render(markdown, console, options)

            with patch.object(_SafeAssistantMarkdown, "__rich_console__", count_render):
                height = visual.get_height(stream.styles, 80)
                strips = visual.render_strips(
                    80,
                    None,
                    stream.visual_style,
                    RenderOptions(stream._get_style, stream.styles),
                )
            return height, len(strips), render_count

    height, strip_count, render_count = anyio.run(scenario)

    assert height == strip_count
    assert render_count == 1


def test_assistant_markdown_strip_cache_retains_only_the_current_width() -> None:
    async def scenario() -> tuple[int, int, int]:
        app = TextualTui()
        async with app.run_test(size=(80, 30)):
            stream = StreamMessage()
            await app.query_one("#transcript", Transcript).mount(stream)
            await stream.replace_markdown(_DOCUMENT)
            visual = stream._selection_visual
            assert isinstance(visual, _SelectableMarkdownVisual)
            render_count = 0
            original_render = _SafeAssistantMarkdown.__rich_console__

            def count_render(
                markdown: _SafeAssistantMarkdown,
                console: Console,
                options: ConsoleOptions,
            ) -> RenderResult:
                nonlocal render_count
                render_count += 1
                yield from original_render(markdown, console, options)

            options = RenderOptions(stream._get_style, stream.styles)
            with patch.object(_SafeAssistantMarkdown, "__rich_console__", count_render):
                wide = visual.render_strips(80, None, stream.visual_style, options)
                narrow = visual.render_strips(28, None, stream.visual_style, options)
                visual.render_strips(80, None, stream.visual_style, options)
            return len(wide), len(narrow), render_count

    wide_height, narrow_height, render_count = anyio.run(scenario)

    assert narrow_height > wide_height
    assert render_count == 3


def test_assistant_markdown_source_revision_releases_the_previous_visual() -> None:
    async def scenario() -> tuple[weakref.ReferenceType[_SelectableMarkdownVisual], str]:
        app = TextualTui()
        async with app.run_test():
            stream = StreamMessage("# First")
            await app.query_one("#transcript", Transcript).mount(stream)
            first_visual = stream._selection_visual
            assert isinstance(first_visual, _SelectableMarkdownVisual)
            first_reference = weakref.ref(first_visual)

            await stream.replace_markdown("# Second")
            del first_visual
            gc.collect()

            return first_reference, stream.source

    first_reference, source = anyio.run(scenario)

    assert source == "# Second"
    assert first_reference() is None


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


def test_assistant_markdown_drag_selection_copies_rendered_structure() -> None:
    source = (
        "## Heading\n\n"
        "Paragraph with `inline code` and more text.\n\n"
        "- first item\n"
        "- second item\n\n"
        '```python\nprint("hello")\n```'
    )

    async def scenario() -> tuple[str | None, list[str]]:
        app = TextualTui()
        copied: list[str] = []
        app.copy_to_clipboard = copied.append  # type: ignore[method-assign]
        async with app.run_test(size=(60, 24)) as pilot:
            stream = StreamMessage(source)
            await app.query_one("#transcript", Transcript).mount(stream)
            await pilot.pause()

            # Drag from the heading through the fenced code row. These pointer
            # coordinates exercise Textual's rendered-row offset lookup rather
            # than constructing a Selection object directly in the test.
            await pilot._post_mouse_events(
                [events.MouseDown], widget=stream, offset=(2, 0), button=1
            )
            await pilot._post_mouse_events(
                [events.MouseMove], widget=stream, offset=(18, 7), button=1
            )
            await pilot._post_mouse_events(
                [events.MouseUp], widget=stream, offset=(18, 7), button=1
            )
            await pilot.pause()
            return app.screen.get_selected_text(), copied

    selected, copied = anyio.run(scenario)
    expected = (
        "Heading\n\n"
        "Paragraph with inline code and more text.\n\n"
        " • first item\n"
        " • second item\n\n"
        '  print("hello")'
    )
    assert selected == expected
    assert copied == [expected]


@pytest.mark.parametrize("theme_name", ["wisp", "wisp-light"])
def test_assistant_markdown_drag_selection_paints_selection_style(
    theme_name: str,
) -> None:
    async def scenario() -> tuple[
        tuple[RichStyle, RichStyle, RichStyle, RichStyle],
        tuple[RichStyle, RichStyle, RichStyle, RichStyle],
    ]:
        app = TextualTui()
        async with app.run_test(size=(60, 24)) as pilot:
            app.theme = theme_name
            stream = StreamMessage(
                "Plain [linked text](https://example.com) with `inline code` and trailing text."
            )
            await app.query_one("#transcript", Transcript).mount(stream)
            await pilot.pause()
            before = (
                _rendered_style_for(stream, "Plain"),
                _rendered_style_for(stream, "linked text"),
                _rendered_style_for(stream, "inline code"),
                _rendered_style_for(stream, "trailing text"),
            )

            await pilot._post_mouse_events(
                [events.MouseDown], widget=stream, offset=(2, 0), button=1
            )
            await pilot._post_mouse_events(
                [events.MouseMove], widget=stream, offset=(35, 0), button=1
            )
            await pilot.pause()
            after = (
                _rendered_style_for(stream, "Plain"),
                _rendered_style_for(stream, "linked text"),
                _rendered_style_for(stream, "inline code"),
                _rendered_style_for(stream, "trailing text"),
            )
            return before, after

    before, after = anyio.run(scenario)
    plain_before, link_before, code_before, trailing_before = before
    plain_after, link_after, code_after, trailing_after = after

    assert plain_after.color == plain_before.color
    assert plain_after.bgcolor != plain_before.bgcolor
    assert link_after.color == link_before.color
    assert link_after.bgcolor != link_before.bgcolor
    assert link_after.meta == link_before.meta
    assert code_after.color == code_before.color
    assert code_after.bgcolor != code_before.bgcolor
    assert code_after.bold == code_before.bold
    assert trailing_after.color == trailing_before.color
    assert trailing_after.bgcolor == trailing_before.bgcolor
    assert trailing_after.bold == trailing_before.bold


def test_assistant_markdown_cached_strips_preserve_link_hover_style() -> None:
    async def scenario() -> tuple[RichStyle, RichStyle, str]:
        app = TextualTui()
        async with app.run_test(size=(60, 20)) as pilot:
            stream = StreamMessage("A [link](https://example.com) here.")
            await app.query_one("#transcript", Transcript).mount(stream)
            await pilot.pause()
            link_x, before = next(
                (x, style)
                for x in range(stream.size.width)
                if "@click" in (style := stream.get_style_at(x, 0)).meta
            )

            await pilot.hover(stream, offset=(link_x, 0))
            await pilot.pause()
            after = stream.get_style_at(link_x, 0)
            return before, after, stream.hover_style.link_id

    before, after, hover_link_id = anyio.run(scenario)

    assert hover_link_id
    assert before.color != after.color
    assert not before.bold
    assert after.bold
    assert before.meta == after.meta


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
            await pilot.pause()  # settle the overflowing transcript layout
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


def test_streaming_markdown_reuses_stable_blocks_with_one_shot_visual_parity() -> None:
    chunks = (
        "# Heading\n\n",
        "Paragraph with **bold**, `code`, and [a link](https://example.com).\n\n",
        "Setext heading\n--------------\n\n",
        "> quote\n>\n> - nested one\n> - nested two\n\n",
        "A | B\n-- | --\n1 | 2\n\n",
        "```python\nprint('closed fence')\n```\n\n",
        "~~~ansi\n\x1b[31mred\x1b[0m\n~~~\n\n",
        "Trailing paragraph with ~~strikethrough~~.",
    )

    async def scenario() -> tuple[list[Segment], list[Segment], int, int, str]:
        app = TextualTui()
        async with app.run_test(size=(80, 40)):
            transcript = app.query_one("#transcript", Transcript)
            streamed = StreamMessage()
            one_shot = StreamMessage()
            await transcript.mount(streamed, one_shot)
            cumulative_work = 0
            full_rebuild_work = 0
            source = ""
            for chunk in chunks:
                source += chunk
                full_rebuild_work += len(source)
                await streamed.append_markdown(chunk)
                cumulative_work += streamed.last_markdown_processed_chars
            await one_shot.replace_markdown(source)
            return (
                _segments(app, streamed),
                _segments(app, one_shot),
                cumulative_work,
                full_rebuild_work,
                streamed.source,
            )

    streamed, one_shot, processed_chars, full_rebuild_chars, source = anyio.run(scenario)

    assert streamed == one_shot
    assert source == "".join(chunks)
    assert processed_chars < full_rebuild_chars


def test_streaming_markdown_falls_back_for_late_reference_definitions() -> None:
    initial = "[linked text][target]\n\nMiddle paragraph.\n\n"
    definition = "[target]: https://example.com/reference\n"

    async def scenario() -> tuple[bool, int, int, str]:
        app = TextualTui()
        async with app.run_test(size=(80, 20)):
            stream = StreamMessage()
            await app.query_one("#transcript", Transcript).mount(stream)
            await stream.append_markdown(initial)
            await stream.append_markdown("Tail paragraph.\n\n")
            assert stream.last_markdown_reused_chars > 0
            await stream.append_markdown(definition)
            segments = _segments(app, stream)
            link_style = _style_for(segments, "linked text")
            return (
                stream.last_markdown_incremental,
                stream.last_markdown_processed_chars,
                len(stream.source),
                str(link_style.meta.get("@click", "")),
            )

    incremental, processed_chars, source_chars, action = anyio.run(scenario)

    assert not incremental
    assert processed_chars == source_chars
    assert action == "open_markdown_link('https://example.com/reference')"


def test_streaming_markdown_caches_closed_fence_highlighting_by_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    syntax_renders = 0
    original_render = Syntax.__rich_console__

    def count_syntax_render(
        syntax: Syntax,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        nonlocal syntax_renders
        syntax_renders += 1
        yield from original_render(syntax, console, options)

    monkeypatch.setattr(Syntax, "__rich_console__", count_syntax_render)

    async def scenario() -> tuple[int, int, int, int]:
        app = TextualTui()
        async with app.run_test(size=(80, 20)):
            stream = StreamMessage()
            await app.query_one("#transcript", Transcript).mount(stream)
            await stream.append_markdown("```python\nprint('cached')\n```\n\n")
            _segments(app, stream, width=80)
            first = syntax_renders
            await stream.append_markdown("Following paragraph.\n\n")
            _segments(app, stream, width=80)
            cached = syntax_renders
            await stream.append_markdown("Another paragraph.\n\n")
            _segments(app, stream, width=80)
            reused = syntax_renders
            _segments(app, stream, width=40)
            resized = syntax_renders
            return first, cached, reused, resized

    first, cached, reused, resized = anyio.run(scenario)

    assert first == 1
    assert cached == 2
    assert reused == cached
    assert resized == reused + 1


def test_streaming_markdown_releases_incremental_caches_after_settlement() -> None:
    async def scenario() -> tuple[int, int, bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 20)):
            stream = StreamMessage()
            await app.query_one("#transcript", Transcript).mount(stream)
            await stream.append_markdown("```python\nprint('cached')\n```\n\nFollowing.\n")
            _segments(app, stream, width=80)
            cached_before = len(stream._code_block_render_cache)
            stream.release_streaming_markdown_caches()
            visual = stream._selection_visual
            assert isinstance(visual, _SelectableMarkdownVisual)
            renderable = visual._markdown_renderable
            assert isinstance(renderable, _SafeAssistantMarkdown)
            return (
                cached_before,
                len(stream._code_block_render_cache),
                renderable.markdown.code_block_render_cache is None,
            )

    cached_before, cached_after, render_cache_released = anyio.run(scenario)

    assert cached_before == 1
    assert cached_after == 0
    assert render_cache_released


def test_streaming_markdown_reuses_theme_configuration_until_style_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_builds = 0
    original_build = StreamMessage._build_markdown_render_config

    def count_config_build(stream: StreamMessage) -> object:
        nonlocal config_builds
        config_builds += 1
        return original_build(stream)

    monkeypatch.setattr(StreamMessage, "_build_markdown_render_config", count_config_build)

    async def scenario() -> tuple[int, int]:
        app = TextualTui()
        async with app.run_test(size=(80, 20)) as pilot:
            stream = StreamMessage()
            await app.query_one("#transcript", Transcript).mount(stream)
            await stream.append_markdown("First paragraph.\n\n")
            await stream.append_markdown("Second paragraph.\n\n")
            before_theme_change = config_builds
            app.theme = "wisp-light"
            await pilot.pause()
            return before_theme_change, config_builds

    before_theme_change, after_theme_change = anyio.run(scenario)

    assert before_theme_change == 1
    assert after_theme_change > before_theme_change


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
