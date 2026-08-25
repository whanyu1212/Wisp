from __future__ import annotations

from dataclasses import dataclass, field

import anyio
import pytest
from rich.console import Console
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
    TerminalWriteDiagnostic,
)
from wisp.tui.history import HistoricalTranscriptMessage
from wisp.tui.rendering import TuiViewSnapshot
from wisp.tui.textual_app import (
    _DisplayedFrame,
    _PendingInputLatency,
    _slash_enter_prefills,
    create_textual_tui,
)
from wisp.tui.textual_renderer import TextualTuiRenderer
from wisp.tui.widgets import DecisionPanel, PromptEditor, StreamMessage, ToolCard, Transcript

pytestmark = pytest.mark.tui


@dataclass
class _Diagnostics:
    markdown: list[MarkdownDrainDiagnostic] = field(default_factory=list)
    display: list[DisplayUpdateDiagnostic] = field(default_factory=list)
    input_latency: list[InputLatencyDiagnostic] = field(default_factory=list)
    terminal_writes: list[TerminalWriteDiagnostic] = field(default_factory=list)

    def record_markdown_drain(self, diagnostic: MarkdownDrainDiagnostic) -> None:
        self.markdown.append(diagnostic)

    def record_display_update(self, diagnostic: DisplayUpdateDiagnostic) -> None:
        self.display.append(diagnostic)

    def record_input_latency(self, diagnostic: InputLatencyDiagnostic) -> None:
        self.input_latency.append(diagnostic)

    def record_terminal_write(self, diagnostic: TerminalWriteDiagnostic) -> None:
        self.terminal_writes.append(diagnostic)


class _FailingDiagnostics:
    def record_markdown_drain(self, _diagnostic: MarkdownDrainDiagnostic) -> None:
        raise RuntimeError("diagnostic sink failed")

    def record_display_update(self, _diagnostic: DisplayUpdateDiagnostic) -> None:
        raise RuntimeError("diagnostic sink failed")

    def record_input_latency(self, _diagnostic: InputLatencyDiagnostic) -> None:
        raise RuntimeError("diagnostic sink failed")

    def record_terminal_write(self, _diagnostic: TerminalWriteDiagnostic) -> None:
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
    assert diagnostics.terminal_writes
    assert all(
        not hasattr(item, "source")
        for item in (*diagnostics.markdown, *diagnostics.display, *diagnostics.terminal_writes)
    )
    assert all(sample.payload_bytes >= 0 for sample in diagnostics.terminal_writes)
    assert all(not sample.observed_driver for sample in diagnostics.terminal_writes)
    assert all(sample.sync_begin_count == 0 for sample in diagnostics.terminal_writes)


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
            input_widget = app.query_one("#input", PromptEditor)
            transcript = app.query_one("#transcript", Transcript)
            for key in ("home", "end", "pageup", "pagedown"):
                assert app._input_event_category(events.Key(key, None)) is None
            input_widget.value = "copy me"
            input_widget.selection = type(input_widget.selection)((0, 0), (0, 7))
            app.copy_to_clipboard = lambda _text: None  # type: ignore[method-assign]
            await pilot.press("ctrl+c")
            input_widget.value = ""
            await pilot.press("alt+enter", "shift+enter", "ctrl+j")
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
            input_widget.value = "one\ntwo\nthree"
            input_widget.cursor_position = 5
            input_widget.selection = type(input_widget.selection)((1, 1), (1, 1))
            for key in (
                "left",
                "right",
                "up",
                "down",
                "ctrl+left",
                "ctrl+right",
                "ctrl+home",
                "ctrl+end",
                "ctrl+a",
                "ctrl+e",
                "shift+left",
                "shift+right",
                "shift+up",
                "shift+down",
                "ctrl+shift+left",
                "ctrl+shift+right",
                "shift+home",
                "shift+end",
                "f6",
                "f7",
            ):
                assert app._input_event_category(events.Key(key, None)) == "cursor"
            input_widget.value = "x"
            input_widget.cursor_position = 0
            input_widget.selection = type(input_widget.selection)((0, 0), (0, 0))
            for key in ("left", "up", "ctrl+left", "ctrl+home", "ctrl+a"):
                assert app._input_event_category(events.Key(key, None)) is None
            assert app._input_event_category(events.Key("down", None)) == "cursor"
            input_widget.cursor_position = 1
            for key in ("right", "down", "ctrl+right", "ctrl+end", "ctrl+e"):
                assert app._input_event_category(events.Key(key, None)) is None
            assert app._input_event_category(events.Key("up", None)) == "cursor"
            input_widget.selection = type(input_widget.selection)((0, 0), (0, 1))
            assert app._input_event_category(events.Key("f6", None)) is None
            assert app._input_event_category(events.Key("f7", None)) is None
            input_widget.value = "delete"
            input_widget.cursor_position = 0
            assert app._input_event_category(events.Key("backspace", None)) is None
            assert app._input_event_category(events.Key("delete", None)) == "typing"
            assert app._input_event_category(events.Key("ctrl+d", None)) == "typing"
            input_widget.cursor_position = len(input_widget.text)
            assert app._input_event_category(events.Key("backspace", None)) == "typing"
            assert app._input_event_category(events.Key("delete", None)) is None
            assert app._input_event_category(events.Key("ctrl+d", None)) is None
            input_widget.selection = type(input_widget.selection)((0, 0), (0, 6))
            assert app._input_event_category(events.Key("backspace", None)) == "typing"
            assert app._input_event_category(events.Key("delete", None)) == "typing"
            assert app._input_event_category(events.Key("ctrl+d", None)) == "typing"
            assert app._input_event_category(events.Key("alt+up", None)) == "typing"
            for key, character in (
                ("ctrl+g", "\x07"),
                ("ctrl+r", "\x12"),
                ("ctrl+t", "\x14"),
            ):
                assert app._input_event_category(events.Key(key, character)) is None
            assert app._input_event_category(events.Paste("private paste")) == "paste"
            origin = transcript.region.offset

            def wheel(
                widget: object,
                *,
                shift: bool = False,
                ctrl: bool = False,
            ) -> events.MouseScrollUp:
                return events.MouseScrollUp(
                    widget=widget,  # type: ignore[arg-type]
                    x=origin.x,
                    y=origin.y,
                    delta_x=0,
                    delta_y=0,
                    button=0,
                    shift=shift,
                    meta=False,
                    ctrl=ctrl,
                    screen_x=origin.x,
                    screen_y=origin.y,
                )

            assert app._input_event_category(wheel(transcript)) == "wheel"
            assert app._input_event_category(wheel(transcript, shift=True)) is None
            assert app._input_event_category(wheel(transcript, ctrl=True)) is None
            transcript.stop_following()
            assert app._input_event_category(wheel(transcript)) is None
            transcript.return_to_latest()
            downward_wheel = events.MouseScrollDown(
                widget=transcript,
                x=origin.x,
                y=origin.y,
                delta_x=0,
                delta_y=0,
                button=0,
                shift=False,
                meta=False,
                ctrl=False,
                screen_x=origin.x,
                screen_y=origin.y,
            )
            assert app._input_event_category(downward_wheel) is None
            horizontal_wheel = events.MouseScrollLeft(
                widget=transcript,
                x=origin.x,
                y=origin.y,
                delta_x=0,
                delta_y=0,
                button=0,
                shift=False,
                meta=False,
                ctrl=False,
                screen_x=origin.x,
                screen_y=origin.y,
            )
            assert app._input_event_category(horizontal_wheel) is None
            assert app._jump_to_latest is not None
            assert app._input_event_category(wheel(app._jump_to_latest)) == "wheel"
            assert app._input_event_category(wheel(app.query_one("#theme-picker"))) is None
            app.action_toggle_contextual_help()
            await pilot.pause()
            assert app._input_event_category(events.Key("pageup", None)) is None
            app.action_toggle_contextual_help()
            await pilot.pause()
            for index in range(40):
                app.write_message(f"diagnostic transcript row {index}", role="system")
            await pilot.pause()
            assert transcript.max_scroll_y > 0
            transcript.scroll_home(animate=False)
            await pilot.pause()
            assert app._input_event_category(events.Key("home", None)) is None
            assert app._input_event_category(events.Key("pageup", None)) is None
            assert app._input_event_category(events.Key("end", None)) == "navigation"
            assert app._input_event_category(events.Key("pagedown", None)) == "navigation"
            assert app._input_event_category(wheel(transcript)) is None
            assert app._input_event_category(downward_wheel) == "wheel"
            transcript.return_to_latest()
            await pilot.pause()
            assert app._input_event_category(events.Key("home", None)) == "navigation"
            assert app._input_event_category(events.Key("pageup", None)) == "navigation"
            assert app._input_event_category(events.Key("end", None)) is None
            assert app._input_event_category(events.Key("pagedown", None)) is None
            assert app._input_event_category(wheel(transcript)) == "wheel"
            assert app._input_event_category(downward_wheel) is None
            card = ToolCard("read", {"path": "README.md"})
            card.set_state("done", detail="complete", full_output="complete")
            await transcript.mount_message(card)
            card.focus()
            await pilot.pause()
            assert app._input_event_category(events.Paste("private paste")) is None
            assert app._input_event_category(events.Key("ctrl+d", None)) == "typing"
            for key in ("enter", "alt+enter", "space", "v", "escape", "p", "n", "l", "x"):
                assert app._input_event_category(events.Key(key, key)) is None
            assert app._input_event_category(events.Key("home", None)) == "navigation"
            assert app._input_event_category(events.Key("ctrl+c", None)) == "cancellation"
            input_widget.focus()
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
            assert app._input_event_category(events.Key("up", None)) is None
            assert app._input_event_category(events.Key("home", None)) is None
            assert app._input_event_category(events.Key("pageup", None)) is None
            assert app._input_event_category(events.Key("down", None)) == "approval"
            decision_panel = app.query_one("#decision-panel", DecisionPanel)
            decision_panel.move_highlight_last()
            assert app._input_event_category(events.Key("down", None)) is None
            assert app._input_event_category(events.Key("end", None)) is None
            assert app._input_event_category(events.Key("pagedown", None)) is None
            assert app._input_event_category(events.Key("up", None)) == "approval"
            assert app._input_event_category(events.Key("home", None)) == "approval"
            assert app._input_event_category(events.Key("pageup", None)) == "approval"
            decision_panel.move_highlight_first()
            decision_panel._mode = "trust"  # noqa: SLF001 - mode-dependent classifier seam
            assert app._input_event_category(events.Key("2", "2")) == "approval"
            assert app._input_event_category(events.Key("3", "3")) is None
            assert app._input_event_category(events.Key("alt+enter", None)) is None
            assert app._input_event_category(events.Key("x", "x")) is None
            assert app._input_event_category(events.Key("ctrl+t", None)) is None
            assert app._input_event_category(events.Key("shift+tab", None)) is None
            assert app._input_event_category(events.Key("ctrl+c", None)) == "cancellation"
            assert app._input_event_category(events.Key("ctrl+d", None)) == "typing"
            await pilot.press("escape")
            renderer.view_updated(TuiViewSnapshot(status="idle", input_hint="wisp> "))
            await pilot.pause()
        return diagnostics

    diagnostics = anyio.run(scenario)

    assert [sample.category for sample in diagnostics.input_latency] == [
        "typing",
        "typing",
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


def test_input_latency_excludes_display_diagnostic_sink_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [3.0]

    class DelayedDisplayDiagnostics(_Diagnostics):
        def record_display_update(self, diagnostic: DisplayUpdateDiagnostic) -> None:
            super().record_display_update(diagnostic)
            now[0] += 10.0

    diagnostics = DelayedDisplayDiagnostics()
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
            now[0] = 3.0
            app._pending_input_latency.append(
                _PendingInputLatency(
                    category="typing",
                    event_time=1.0,
                    received_at=1.0,
                    handled_at=2.0,
                )
            )
            monkeypatch.setattr("wisp.tui.textual_app.perf_counter", lambda: now[0])
            app._display(app.screen, "frame")

    anyio.run(scenario)

    assert len(diagnostics.input_latency) == 1
    sample = diagnostics.input_latency[0]
    assert sample.handler_seconds == 1.0
    assert sample.queued_seconds == 1.0
    assert sample.display_seconds == 0.0
    assert sample.total_seconds == 2.0


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


class _FakeDriver:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.flush_count = 0

    def write(self, data: str) -> None:
        self.writes.append(data)

    def flush(self) -> None:
        self.flush_count += 1


def test_classify_terminal_write_uses_prefixes_only() -> None:
    from wisp.tui.terminal_writes import classify_terminal_write, windows_chunk_count

    assert classify_terminal_write("\x1b[?2026h", in_display=True) == "sync_begin"
    assert classify_terminal_write("\x1b[?2026l", in_display=True) == "sync_end"
    assert classify_terminal_write("\x1b]52;c;QUJD\a", in_display=False) == "osc52"
    assert classify_terminal_write("\x1b[?2026$p", in_display=False) == "mode_query"
    assert classify_terminal_write("\x07", in_display=False) == "bell"
    assert classify_terminal_write("cells", in_display=True) == "payload"
    assert classify_terminal_write("cells", in_display=False) == "other"
    assert windows_chunk_count(0) == 0
    assert windows_chunk_count(1) == 1
    assert windows_chunk_count(8192) == 1
    assert windows_chunk_count(8193) == 2
    multibyte_payload = "界" * 4100
    assert len(multibyte_payload.encode("utf-8")) > 8192
    assert windows_chunk_count(len(multibyte_payload)) == 1


def test_headless_write_model_chunks_windows_payloads_by_character_count() -> None:
    from wisp.tui.terminal_writes import TerminalWriteObserver

    diagnostics = _Diagnostics()
    observer = TerminalWriteObserver(diagnostics)
    observer.begin_frame(None)
    observer.finish_frame(
        "界" * 4100,
        headless=True,
        sync_available=False,
        console=Console(width=10000, color_system=None),
    )

    [sample] = diagnostics.terminal_writes
    assert sample.payload_bytes > 8192
    assert sample.windows_chunk_count == 1


def test_headless_write_model_discards_setup_before_deferred_measurement() -> None:
    from wisp.tui.terminal_writes import TerminalWriteObserver

    diagnostics = _Diagnostics()
    observer = TerminalWriteObserver(diagnostics, defer_headless_models=True)
    observer.begin_frame(None)
    observer.finish_frame(
        "setup",
        headless=True,
        sync_available=False,
        console=Console(color_system=None),
    )
    observer.discard_deferred_frames()
    observer.begin_frame(None)
    observer.finish_frame(
        "measured",
        headless=True,
        sync_available=False,
        console=Console(color_system=None),
    )

    assert diagnostics.terminal_writes == []
    observer.flush_deferred_frames()

    [sample] = diagnostics.terminal_writes
    assert sample.payload_bytes == len(b"measured\n")
    assert sample.posix_write_count == 1
    assert not sample.observed_driver


def test_terminal_write_observer_requires_sink_opt_in() -> None:
    class DisplayOnlyDiagnostics:
        def record_markdown_drain(self, _diagnostic: MarkdownDrainDiagnostic) -> None:
            return

        def record_display_update(self, _diagnostic: DisplayUpdateDiagnostic) -> None:
            return

        def record_input_latency(self, _diagnostic: InputLatencyDiagnostic) -> None:
            return

    app, _renderer = create_textual_tui(diagnostics=DisplayOnlyDiagnostics())

    assert app._terminal_writes is None


def test_headless_display_reports_a_write_model_without_a_live_driver(
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
            changed = Strip([Segment("x" + " " * (size.width - 1))], size.width)
            blank = Strip([Segment(" " * size.width)], size.width)
            diagnostics.terminal_writes.clear()
            app._display(
                screen,
                LayoutUpdate(
                    [[changed], *([blank] for _ in range(size.height - 1))],
                    Region(0, 0, size.width, size.height),
                ),
            )

    anyio.run(scenario)

    frames = [sample for sample in diagnostics.terminal_writes if not sample.out_of_band]
    assert frames
    sample = frames[-1]
    assert sample.payload_bytes > 0
    assert sample.posix_write_count == 1
    assert sample.windows_chunk_count >= 1
    assert not sample.observed_driver
    assert sample.sync_begin_count == 0
    assert sample.sync_end_count == 0
    assert not hasattr(sample, "payload")
    assert not hasattr(sample, "text")


def test_headless_batched_display_does_not_synthesize_writes(
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
            diagnostics.terminal_writes.clear()
            app._batch_count += 1
            try:
                app._display(app.screen, "suppressed")
            finally:
                app._batch_count -= 1

    anyio.run(scenario)

    assert diagnostics.terminal_writes == []


def test_silent_live_driver_does_not_synthesize_headless_writes() -> None:
    from wisp.tui.terminal_writes import TerminalWriteObserver

    diagnostics = _Diagnostics()
    driver = _FakeDriver()
    observer = TerminalWriteObserver(diagnostics)
    observer.begin_frame(driver)
    observer.finish_frame(
        "suppressed",
        headless=False,
        sync_available=False,
        console=_write_console(),
    )

    assert diagnostics.terminal_writes == []


def test_observed_driver_counts_balanced_sync_writes_and_restores_methods() -> None:
    from wisp.tui.terminal_writes import TerminalWriteObserver

    diagnostics = _Diagnostics()
    driver = _FakeDriver()
    observer = TerminalWriteObserver(diagnostics)
    original_write = driver.write
    original_flush = driver.flush
    observer.attach(driver)
    assert driver.write is not original_write

    observer.begin_frame(driver)
    driver.write("\x1b[?2026h")
    driver.write("payload")
    driver.write("\x1b[?2026l")
    driver.flush()
    observer.finish_frame(
        object(),
        headless=False,
        sync_available=True,
        console=_write_console(),
    )
    observer.detach()

    assert getattr(driver.write, "__func__", driver.write) is _FakeDriver.write
    assert getattr(driver.flush, "__func__", driver.flush) is _FakeDriver.flush
    assert original_write.__func__ is _FakeDriver.write
    assert original_flush.__func__ is _FakeDriver.flush
    assert driver.writes == ["\x1b[?2026h", "payload", "\x1b[?2026l"]
    assert driver.flush_count == 1
    sample = diagnostics.terminal_writes[-1]
    assert sample.observed_driver
    assert sample.sync_available
    assert sample.write_count == 3
    assert sample.flush_count == 1
    assert sample.payload_bytes == len(b"payload")
    assert sample.posix_write_count == 1
    assert sample.windows_chunk_count == 1
    assert sample.sync_begin_count == 1
    assert sample.sync_end_count == 1
    assert sample.writes_inside_sync == 1
    assert sample.writes_outside_sync == 0
    assert not sample.out_of_band


def test_observer_restores_driver_when_write_raises() -> None:
    from wisp.tui.terminal_writes import TerminalWriteObserver

    class RaisingDriver(_FakeDriver):
        def write(self, data: str) -> None:
            raise RuntimeError("driver write failed")

    diagnostics = _Diagnostics()
    driver = RaisingDriver()
    observer = TerminalWriteObserver(diagnostics)
    original_write = driver.write
    observer.attach(driver)
    observer.begin_frame(driver)
    with pytest.raises(RuntimeError, match="driver write failed"):
        driver.write("payload")
    observer.detach()
    assert getattr(driver.write, "__func__", driver.write) is RaisingDriver.write
    assert original_write.__func__ is RaisingDriver.write
    with pytest.raises(RuntimeError, match="driver write failed"):
        driver.write("still original")


def test_copy_to_clipboard_is_not_charged_to_the_current_frame() -> None:
    from wisp.tui.terminal_writes import TerminalWriteObserver

    diagnostics = _Diagnostics()
    driver = _FakeDriver()
    observer = TerminalWriteObserver(diagnostics)
    observer.attach(driver)
    driver.write("\x1b]52;c;QUJD\a")
    observer.begin_frame(driver)
    driver.write("payload")
    driver.flush()
    observer.finish_frame(
        "frame",
        headless=False,
        sync_available=False,
        console=_write_console(),
    )
    observer.detach()

    out_of_band = [sample for sample in diagnostics.terminal_writes if sample.out_of_band]
    frames = [sample for sample in diagnostics.terminal_writes if not sample.out_of_band]
    assert len(out_of_band) == 1
    assert out_of_band[0].out_of_band_kind == "osc52"
    assert out_of_band[0].payload_bytes == 0
    assert frames[-1].write_count == 1
    assert frames[-1].writes_outside_sync == 1
    assert not frames[-1].out_of_band


def test_record_terminal_write_is_optional_and_isolates_sink_failures() -> None:
    from wisp.tui.diagnostics import record_terminal_write
    from wisp.tui.terminal_writes import TerminalWriteObserver

    class OptionalSink:
        def record_markdown_drain(self, _diagnostic: MarkdownDrainDiagnostic) -> None:
            return

        def record_display_update(self, _diagnostic: DisplayUpdateDiagnostic) -> None:
            return

        def record_input_latency(self, _diagnostic: InputLatencyDiagnostic) -> None:
            return

    class FailingWriteSink(_FailingDiagnostics):
        def record_terminal_write(self, _diagnostic: TerminalWriteDiagnostic) -> None:
            raise RuntimeError("terminal write sink failed")

    sample = TerminalWriteDiagnostic(
        display_kind="other",
        sync_available=False,
        write_count=1,
        flush_count=0,
        payload_bytes=4,
        max_write_bytes=4,
        posix_write_count=1,
        windows_chunk_count=1,
        sync_begin_count=0,
        sync_end_count=0,
        writes_inside_sync=0,
        writes_outside_sync=1,
        observed_driver=False,
        out_of_band=False,
        out_of_band_kind=None,
    )
    record_terminal_write(OptionalSink(), sample)
    observer = TerminalWriteObserver(FailingWriteSink())
    observer.finish_frame(
        "frame",
        headless=True,
        sync_available=False,
        console=_write_console(),
    )


def _write_console():
    from rich.console import Console

    return Console(record=True, width=20, height=5)
