from __future__ import annotations

from dataclasses import dataclass, field

import anyio
import pytest
from rich.segment import Segment
from textual import events
from textual._compositor import ChopsUpdate, LayoutUpdate
from textual.app import App
from textual.geometry import Region
from textual.strip import Strip

from wisp.events import ToolApprovalRequested, ToolCallRequested, ToolResultReady
from wisp.tui.commands import SlashCommandSpec
from wisp.tui.diagnostics import (
    DisplayUpdateDiagnostic,
    InputLatencyDiagnostic,
    MarkdownDrainDiagnostic,
)
from wisp.tui.history import HistoricalTranscriptMessage
from wisp.tui.rendering import TuiViewSnapshot
from wisp.tui.textual_app import _DisplayedFrame, _slash_enter_prefills, create_textual_tui
from wisp.tui.textual_renderer import TextualTuiRenderer
from wisp.tui.widgets import PromptEditor, StreamMessage, ToolCard, Transcript

pytestmark = pytest.mark.tui


@dataclass
class _Diagnostics:
    markdown: list[MarkdownDrainDiagnostic] = field(default_factory=list)
    display: list[DisplayUpdateDiagnostic] = field(default_factory=list)
    input_latency: list[InputLatencyDiagnostic] = field(default_factory=list)

    def record_markdown_drain(self, diagnostic: MarkdownDrainDiagnostic) -> None:
        self.markdown.append(diagnostic)

    def record_display_update(self, diagnostic: DisplayUpdateDiagnostic) -> None:
        self.display.append(diagnostic)

    def record_input_latency(self, diagnostic: InputLatencyDiagnostic) -> None:
        self.input_latency.append(diagnostic)


class _FailingDiagnostics:
    def record_markdown_drain(self, _diagnostic: MarkdownDrainDiagnostic) -> None:
        raise RuntimeError("diagnostic sink failed")

    def record_display_update(self, _diagnostic: DisplayUpdateDiagnostic) -> None:
        raise RuntimeError("diagnostic sink failed")

    def record_input_latency(self, _diagnostic: InputLatencyDiagnostic) -> None:
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


def test_input_diagnostics_measure_handler_queue_and_first_display_without_values() -> None:
    async def scenario() -> _Diagnostics:
        diagnostics = _Diagnostics()
        app, _renderer = create_textual_tui(diagnostics=diagnostics)
        async with app.run_test() as pilot:
            await pilot.press(*"private-input")
            await pilot.pause()
            await pilot.press("left")
            await pilot.pause()
        return diagnostics

    diagnostics = anyio.run(scenario)

    assert [sample.category for sample in diagnostics.input_latency] == [
        *("typing" for _ in "private-input"),
        "cursor",
    ]
    for sample in diagnostics.input_latency:
        assert sample.handler_seconds >= 0
        assert sample.queued_seconds >= 0
        assert sample.display_seconds >= 0
        assert sample.total_seconds >= (
            sample.handler_seconds + sample.queued_seconds + sample.display_seconds
        )
        assert sample.display_kind in {"layout", "chops", "other"}
        assert not hasattr(sample, "key")
        assert not hasattr(sample, "value")
        assert not hasattr(sample, "content")


def test_input_diagnostics_classify_contextual_key_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> _Diagnostics:
        diagnostics = _Diagnostics()
        app, renderer = create_textual_tui(diagnostics=diagnostics)
        assert isinstance(renderer, TextualTuiRenderer)
        async with app.run_test() as pilot:
            await pilot.press("home", "end")
            await pilot.pause()
            input_widget = app.query_one("#input", PromptEditor)
            input_widget.value = "copy me"
            input_widget.selection = type(input_widget.selection)((0, 0), (0, 7))
            app.copy_to_clipboard = lambda _text: None  # type: ignore[method-assign]
            await pilot.press("ctrl+c")
            input_widget.value = ""
            await pilot.press("alt+enter")
            await pilot.pause()
            renderer.view_updated(
                TuiViewSnapshot(
                    status="running", input_hint="wisp(running)> ", input_mode="running"
                )
            )
            input_widget.value = "follow up"
            await pilot.press("alt+enter")
            await pilot.pause()
            app.show_theme_picker()
            assert app._input_event_category(events.Key("escape", None)) is None
            app.hide_theme_picker()
            assert _slash_enter_prefills(
                "/d",
                SlashCommandSpec(
                    command="/deploy",
                    description="Deploy",
                    prefill_on_partial_enter=True,
                ),
            )
            with monkeypatch.context() as context:
                context.setattr(app, "_file_picker_is_active", lambda: False)
                context.setattr(app, "_slash_menu_prefills_on_enter", lambda: True)
                assert app._input_event_category(events.Key("enter", None)) is None
            assert app._input_event_category(events.Key("up", None)) == "cursor"
            assert app._input_event_category(events.Key("down", None)) == "cursor"
            assert app._input_event_category(events.Key("ctrl+home", None)) == "cursor"
            assert app._input_event_category(events.Key("ctrl+end", None)) == "cursor"
            with monkeypatch.context() as context:
                context.setattr(app, "_file_picker_is_active", lambda: True)
                context.setattr(app, "_file_tree_is_active", lambda: True)
                context.setattr(app, "_suggestion_menu_is_open", lambda: True)
                assert app._input_event_category(events.Key("enter", None)) is None
                assert app._input_event_category(events.Key("up", None)) is None
                assert app._input_event_category(events.Key("down", None)) is None
                assert app._input_event_category(events.Key("left", None)) is None
                assert app._input_event_category(events.Key("right", None)) is None
                assert app._input_event_category(events.Key("escape", None)) is None
            renderer.approval_request(
                ToolApprovalRequested(
                    call_id="diagnostic-approval",
                    name="bash",
                    arguments={"command": "printf diagnostic"},
                    safety="command",
                )
            )
            await pilot.pause()
            assert app._input_event_category(events.Key("down", None)) == "approval"
            assert app._input_event_category(events.Key("ctrl+t", None)) is None
            assert app._input_event_category(events.Key("shift+tab", None)) is None
            assert app._input_event_category(events.Key("ctrl+c", None)) == "cancellation"
            await pilot.press("escape")
            renderer.view_updated(TuiViewSnapshot(status="idle", input_hint="wisp> "))
            await pilot.pause()
        return diagnostics

    diagnostics = anyio.run(scenario)

    assert [sample.category for sample in diagnostics.input_latency] == [
        "navigation",
        "navigation",
        "typing",
        "submission",
        "approval",
    ]


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


def test_pending_tool_tick_stays_a_chops_update() -> None:
    async def scenario() -> list[DisplayUpdateDiagnostic]:
        diagnostics = _Diagnostics()
        app, renderer = create_textual_tui(diagnostics=diagnostics)
        assert isinstance(renderer, TextualTuiRenderer)
        async with app.run_test() as pilot:
            renderer.event(ToolCallRequested(call_id="tick", name="grep", arguments={}))
            await pilot.pause()
            card = app.query_one(ToolCard)
            diagnostics.display.clear()
            card._tick()
            await pilot.pause()
            return diagnostics.display

    updates = anyio.run(scenario)

    assert updates
    assert any(update.kind == "chops" for update in updates)
    assert all(update.kind != "layout" for update in updates)


def test_tool_card_updates_omit_unchanged_terminal_padding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    displayed: list[object | None] = []

    def capture_display(
        _app: App[object],
        _screen: object,
        renderable: object | None,
    ) -> None:
        displayed.append(renderable)

    monkeypatch.setattr(App, "_display", capture_display)

    async def scenario() -> tuple[int, list[object | None], list[object | None]]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 18)) as pilot:
            renderer.event(ToolCallRequested(call_id="card", name="read", arguments={}))
            await pilot.pause()
            await pilot.pause()
            card = app.query_one(ToolCard)

            displayed.clear()
            card._tick()
            await pilot.pause()
            await pilot.pause()
            tick_updates = displayed.copy()

            displayed.clear()
            renderer.event(
                ToolResultReady(
                    call_id="card",
                    name="read",
                    output="ok",
                    is_error=False,
                )
            )
            await pilot.pause()
            await pilot.pause()
            return app.screen.outer_size.width, tick_updates, displayed.copy()

    width, tick_updates, result_updates = anyio.run(scenario)

    for updates in (tick_updates, result_updates):
        chops = [update for update in updates if isinstance(update, ChopsUpdate)]
        assert len(chops) == 1
        assert all(x2 < width for update in chops for _y, _x1, x2 in update.spans)
        assert all(update is None or isinstance(update, ChopsUpdate) for update in updates)


def test_display_diagnostics_report_filtered_layout_outcome(
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
            changed = Strip([Segment("x" + " " * (size.width - 1))], size.width)
            app._displayed_screen = screen
            app._displayed_cursor_position = size.clamp_offset(app.cursor_position)
            app._displayed_frame = _DisplayedFrame(
                size=size,
                rows=[blank for _ in range(size.height)],
            )
            diagnostics.display.clear()
            app._display(
                screen,
                LayoutUpdate([[blank] for _ in range(size.height)], size.region),
            )
            app._display(
                screen,
                LayoutUpdate(
                    [[changed], *([blank] for _ in range(size.height - 1))],
                    Region(0, 0, size.width, size.height),
                ),
            )

    anyio.run(scenario)

    duplicate, changed = diagnostics.display
    assert duplicate.kind == "none"
    assert duplicate.emitted_spans == 0
    assert changed.kind == "chops"
    assert changed.input_spans == changed.emitted_spans == 1


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
