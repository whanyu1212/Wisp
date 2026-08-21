from __future__ import annotations

from dataclasses import dataclass, field

import anyio
import pytest
from rich.segment import Segment
from textual._compositor import ChopsUpdate
from textual.app import App
from textual.strip import Strip

from wisp.tui.diagnostics import DisplayUpdateDiagnostic, MarkdownDrainDiagnostic
from wisp.tui.history import HistoricalTranscriptMessage
from wisp.tui.textual_app import _DisplayedFrame, create_textual_tui
from wisp.tui.textual_renderer import TextualTuiRenderer
from wisp.tui.widgets import StreamMessage, Transcript

pytestmark = pytest.mark.tui


@dataclass
class _Diagnostics:
    markdown: list[MarkdownDrainDiagnostic] = field(default_factory=list)
    display: list[DisplayUpdateDiagnostic] = field(default_factory=list)

    def record_markdown_drain(self, diagnostic: MarkdownDrainDiagnostic) -> None:
        self.markdown.append(diagnostic)

    def record_display_update(self, diagnostic: DisplayUpdateDiagnostic) -> None:
        self.display.append(diagnostic)


class _FailingDiagnostics:
    def record_markdown_drain(self, _diagnostic: MarkdownDrainDiagnostic) -> None:
        raise RuntimeError("diagnostic sink failed")

    def record_display_update(self, _diagnostic: DisplayUpdateDiagnostic) -> None:
        raise RuntimeError("diagnostic sink failed")


def test_tui_diagnostics_report_only_numeric_stream_metadata() -> None:
    async def scenario() -> tuple[_Diagnostics, str]:
        diagnostics = _Diagnostics()
        app, renderer = create_textual_tui(diagnostics=diagnostics)
        assert isinstance(renderer, TextualTuiRenderer)
        async with app.run_test() as pilot:
            renderer.token_delta("private streamed content")
            await app.wait_for_stream_idle()
            renderer.end_token_stream_with_content("private streamed content")
            await app.wait_for_stream_idle()
            await pilot.pause()
            source = app.query_one(StreamMessage).source
        return diagnostics, source

    diagnostics, source = anyio.run(scenario)

    assert source == "private streamed content"
    assert diagnostics.markdown
    sample = diagnostics.markdown[0]
    assert sample.appended_chars == len(source)
    assert sample.appended_bytes == len(source.encode("utf-8"))
    assert sample.resulting_source_chars == len(source)
    assert sample.processed_source_chars == len(source)
    assert sample.reused_source_chars == 0
    assert sample.incremental
    assert sample.render_seconds >= 0
    assert sample.succeeded
    assert diagnostics.display
    assert all(
        not hasattr(item, "source") for item in (*diagnostics.markdown, *diagnostics.display)
    )


def test_display_diagnostics_report_exact_suppression_and_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostics = _Diagnostics()
    app, _renderer = create_textual_tui(diagnostics=diagnostics)

    def discard_display(
        _app: App[object],
        _screen: object,
        _renderable: object,
    ) -> None:
        return

    monkeypatch.setattr(App, "_display", discard_display)

    async def scenario() -> None:
        async with app.run_test():
            screen = app.screen
            size = screen.outer_size
            blank = Strip([Segment(" " * size.width)], size.width)
            app._displayed_screen = screen
            app._displayed_cursor_position = size.clamp_offset(app.cursor_position)

            app._displayed_frame = _DisplayedFrame(
                size=size,
                rows=[blank for _ in range(size.height)],
            )
            duplicate = ChopsUpdate(
                [{0: blank}, *({} for _ in range(size.height - 1))],
                [(0, 0, size.width)],
                [[size.width], *([] for _ in range(size.height - 1))],
            )
            diagnostics.display.clear()
            app._display(screen, duplicate)

            app._displayed_frame = _DisplayedFrame(
                size=size,
                rows=[blank for _ in range(size.height)],
            )
            incomplete = ChopsUpdate(
                [{0: blank.crop(0, size.width - 1)}, *({} for _ in range(size.height - 1))],
                [(0, 0, size.width)],
                [[size.width - 1], *([] for _ in range(size.height - 1))],
            )
            app._display(screen, incomplete)

    anyio.run(scenario)

    duplicate, incomplete = diagnostics.display
    assert duplicate.input_spans == 1
    assert duplicate.emitted_spans == 0
    assert duplicate.suppressed_spans == 1
    assert duplicate.frame_cache == "retained"
    assert not duplicate.fail_open
    assert incomplete.input_spans == incomplete.emitted_spans == 1
    assert incomplete.suppressed_spans == 0
    assert incomplete.frame_cache == "fail-open"
    assert incomplete.fail_open


def test_tui_diagnostic_sink_failures_do_not_interrupt_rendering() -> None:
    async def scenario() -> str:
        app, renderer = create_textual_tui(diagnostics=_FailingDiagnostics())
        assert isinstance(renderer, TextualTuiRenderer)
        async with app.run_test() as pilot:
            renderer.token_delta("still visible")
            await app.wait_for_stream_idle()
            renderer.end_token_stream_with_content("still visible")
            await app.wait_for_stream_idle()
            await pilot.pause()
            return app.query_one(StreamMessage).source

    assert anyio.run(scenario) == "still visible"


def test_tui_diagnostics_record_failed_incremental_markdown_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_append = StreamMessage.append_markdown
    failed = False

    async def fail_once(widget: StreamMessage, fragment: str) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("simulated incremental failure")
        await original_append(widget, fragment)

    monkeypatch.setattr(StreamMessage, "append_markdown", fail_once)

    async def scenario() -> tuple[_Diagnostics, str]:
        diagnostics = _Diagnostics()
        app, renderer = create_textual_tui(diagnostics=diagnostics)
        assert isinstance(renderer, TextualTuiRenderer)
        async with app.run_test() as pilot:
            renderer.token_delta("authoritative response")
            await app.wait_for_stream_idle()
            renderer.end_token_stream_with_content("authoritative response")
            await app.wait_for_stream_idle()
            await pilot.pause()
            return diagnostics, app.query_one(StreamMessage).source

    diagnostics, source = anyio.run(scenario)

    assert source == "authoritative response"
    assert any(not sample.succeeded for sample in diagnostics.markdown)


def test_history_prepend_diagnostics_never_report_an_escaped_unsettled_paint() -> None:
    async def scenario() -> list[DisplayUpdateDiagnostic]:
        diagnostics = _Diagnostics()
        app, renderer = create_textual_tui(diagnostics=diagnostics)
        assert isinstance(renderer, TextualTuiRenderer)
        async with app.run_test(size=(60, 12)) as pilot:
            renderer.replace_history_entries(
                tuple(
                    HistoricalTranscriptMessage(role="assistant", content=f"current {index}")
                    for index in range(30)
                ),
                session_label="Diagnostic session",
            )
            await app.wait_for_history_render()
            transcript = app.query_one(Transcript)
            transcript.scroll_to(y=8, animate=False)
            await pilot.pause()
            diagnostics.display.clear()
            renderer.prepend_history_entries(
                tuple(
                    HistoricalTranscriptMessage(role="user", content=f"older {index}")
                    for index in range(12)
                )
            )
            with anyio.fail_after(5):
                while (
                    app._history_prepend_anchor is not None or app._history_prepend_paint_suppressed
                ):
                    await pilot.pause()
            await pilot.pause()
        return diagnostics.display

    display = anyio.run(scenario)

    assert any(item.history_prepend_suppressed for item in display)
    assert not any(
        item.history_prepend_unsettled and not item.history_prepend_suppressed for item in display
    )
