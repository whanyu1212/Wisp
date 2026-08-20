from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest
from textual.content import Content
from textual.widget import Widget
from textual.widgets import DataTable, Label, LoadingIndicator, OptionList

from wisp.events import RpcSessionSummary, ToolApprovalRequested
from wisp.tui.history import HistoricalTranscriptMessage
from wisp.tui.overlay import TranscriptViewportState
from wisp.tui.textual_app import create_textual_tui
from wisp.tui.widgets import (
    DecisionPanel,
    LineMessage,
    OperationIndicator,
    PromptEditor,
    SessionPicker,
    Transcript,
)

pytestmark = pytest.mark.tui


def _session(
    session_id: str,
    *,
    name: str | None = None,
    updated_at: datetime | None = None,
    entry_count: int = 1,
) -> RpcSessionSummary:
    return RpcSessionSummary(
        session_id=session_id,
        session_path=Path(f"/tmp/{session_id}.jsonl"),
        updated_at=updated_at or datetime(2026, 1, 1, tzinfo=UTC),
        entry_count=entry_count,
        name=name,
    )


def test_session_picker_preserves_rpc_order_and_highlights_selected_session() -> None:
    sessions = (
        _session("newer", name="Newer", updated_at=datetime(2026, 2, 1, tzinfo=UTC)),
        _session("selected", name="Selected"),
    )

    async def scenario() -> tuple[list[str], int | None]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            renderer.session_picker_request(sessions, selected_session_id="selected")
            await pilot.pause()
            options = app.query_one("#session-picker-options", OptionList)
            labels = [
                str(options.get_option_at_index(index).prompt)
                for index in range(options.option_count)
            ]
            return labels, options.highlighted

    labels, highlighted = anyio.run(scenario)
    assert labels[0].startswith("  Newer")
    assert labels[1].startswith("● Selected")
    assert highlighted == 1


def test_session_picker_uses_a_data_table_at_the_wide_breakpoint() -> None:
    sessions = (
        _session("newer", name="Newer", updated_at=datetime(2026, 2, 1, tzinfo=UTC)),
        _session("selected", name="Selected", entry_count=42),
    )

    async def scenario() -> tuple[bool, bool, int, tuple[str, ...], list[str]]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(96, 24)) as pilot:
            renderer.session_picker_request(sessions, selected_session_id="selected")
            await pilot.pause()
            table = app.query_one("#session-picker-table", DataTable)
            options = app.query_one("#session-picker-options", OptionList)
            row = table.get_row_at(1)
            assert all(isinstance(cell, Content) for cell in row)
            return (
                table.display,
                options.display,
                table.cursor_row,
                tuple(str(column.key.value) for column in table.ordered_columns),
                [cell.plain for cell in row if isinstance(cell, Content)],
            )

    table_visible, options_visible, cursor_row, columns, row = anyio.run(scenario)
    assert table_visible
    assert not options_visible
    assert cursor_row == 1
    assert columns == ("current", "session", "updated", "entries", "path")
    assert row == ["●", "Selected", "2026-01-01T00:00+00:00", "42", "/tmp/selected.jsonl"]


def test_session_picker_uses_the_compact_list_below_the_wide_breakpoint() -> None:
    async def scenario() -> tuple[bool, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(95, 24)) as pilot:
            renderer.session_picker_request((_session("target"),), selected_session_id=None)
            await pilot.pause()
            table = app.query_one("#session-picker-table", DataTable)
            options = app.query_one("#session-picker-options", OptionList)
            return table.display, options.display

    table_visible, options_visible = anyio.run(scenario)
    assert not table_visible
    assert options_visible


@pytest.mark.parametrize("theme", ["wisp", "wisp-light"])
def test_session_picker_table_fits_the_wide_breakpoint(theme: str) -> None:
    async def scenario() -> tuple[int, int, int, int, int, int, float]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(96, 24)) as pilot:
            app.theme = theme
            renderer.session_picker_request((_session("target"),), selected_session_id=None)
            await pilot.pause()
            table = app.query_one("#session-picker-table", DataTable)
            return (
                table.region.x,
                table.region.y,
                table.region.right,
                table.region.bottom,
                app.size.width,
                app.size.height,
                table.max_scroll_x,
            )

    left, top, right, bottom, width, height, max_scroll_x = anyio.run(scenario)
    assert 0 <= left < right <= width
    assert 0 <= top < bottom <= height
    assert max_scroll_x == 0


def test_session_picker_table_mouse_selection_uses_the_resume_command() -> None:
    sessions = (_session("first"), _session("target"))

    async def scenario() -> str:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(100, 24)) as pilot:
            renderer.session_picker_request(sessions, selected_session_id=None)
            await pilot.pause()
            # One click moves the row cursor; the second selects it through the
            # same DataTable.RowSelected path as Enter.
            await pilot.click("#session-picker-table", offset=(5, 2), times=2)
            await pilot.pause()
            answer = await app.read_prompt("wisp> ")
            renderer.session_switch_finished()
            return answer

    assert anyio.run(scenario) == "/resume target"


def test_session_picker_preserves_the_selected_session_across_layout_resizes() -> None:
    sessions = (_session("first"), _session("selected"), _session("last"))

    async def scenario() -> tuple[bool, bool, int | None, int, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(100, 24)) as pilot:
            renderer.session_picker_request(sessions, selected_session_id="selected")
            await pilot.pause()
            table = app.query_one("#session-picker-table", DataTable)
            options = app.query_one("#session-picker-options", OptionList)
            await pilot.resize_terminal(95, 24)
            await pilot.pause()
            narrow_selected = options.highlighted
            narrow_visible = options.display
            await pilot.resize_terminal(100, 24)
            await pilot.pause()
            return table.display, narrow_visible, narrow_selected, table.cursor_row, table.has_focus

    table_visible, options_visible, narrow_selected, wide_selected, table_focused = anyio.run(
        scenario
    )
    assert table_visible
    assert options_visible
    assert narrow_selected == wide_selected == 1
    assert table_focused


def test_session_picker_table_navigation_is_bounded() -> None:
    sessions = tuple(_session(f"session-{index}") for index in range(20))

    async def scenario() -> tuple[int, int, int, int]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(100, 24)) as pilot:
            renderer.session_picker_request(sessions, selected_session_id=None)
            await pilot.pause()
            table = app.query_one("#session-picker-table", DataTable)
            await pilot.press("pagedown")
            await pilot.pause()
            after_page_down = table.cursor_row
            await pilot.press("pageup")
            await pilot.pause()
            after_page_up = table.cursor_row
            await pilot.press("end")
            await pilot.pause()
            after_end = table.cursor_row
            await pilot.press("home")
            await pilot.pause()
            return after_page_down, after_page_up, after_end, table.cursor_row

    after_page_down, after_page_up, after_end, after_home = anyio.run(scenario)
    assert after_page_down > 0
    assert after_page_up == 0
    assert after_end == 19
    assert after_home == 0


def test_session_catalog_loading_hides_composer_and_preserves_draft() -> None:
    async def scenario() -> tuple[str, bool, bool, str, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            indicator = app.query_one("#operation-indicator", OperationIndicator)
            label = app.query_one("#operation-indicator-label", Label)
            spinner = app.query_one("#operation-indicator-spinner", LoadingIndicator)
            editor.value = "draft while catalog loads"
            renderer.session_catalog_started()
            await pilot.pause()
            hidden = not editor.display
            content = label.render()
            assert isinstance(content, Content)
            assert spinner.display
            await pilot.press("enter", "escape")
            await pilot.pause()
            draft = editor.value
            renderer.session_catalog_finished()
            await pilot.pause()
            return draft, hidden, editor.has_focus, content.plain, indicator.is_open

    draft, hidden, focused, label, visible_after_finish = anyio.run(scenario)
    assert draft == "draft while catalog loads"
    assert hidden is True
    assert focused is True
    assert label == "Loading sessions…"
    assert visible_after_finish is False


def test_history_hydration_indicator_covers_partial_transcript_and_updates_in_place() -> None:
    async def scenario() -> tuple[str, bool, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            indicator = app.query_one("#operation-indicator", OperationIndicator)
            label = app.query_one("#operation-indicator-label", Label)

            renderer.history_hydration_started()
            await pilot.pause()
            covered_before = indicator.has_class("-covers-transcript")
            renderer.history_hydration_progress("Preparing transcript… 16 / 40 cards")
            await pilot.pause()
            content = label.render()
            assert isinstance(content, Content)
            covered_after = indicator.has_class("-covers-transcript")
            renderer.history_hydration_finished()
            await pilot.pause()
            return content.plain, covered_before and covered_after, indicator.is_open

    label, covered, visible_after_finish = anyio.run(scenario)

    assert label == "Preparing transcript… 16 / 40 cards"
    assert covered is True
    assert visible_after_finish is False


def test_session_operation_indicator_does_not_obscure_an_approval() -> None:
    async def scenario() -> tuple[bool, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            indicator = app.query_one("#operation-indicator", OperationIndicator)
            renderer.session_catalog_started()
            await pilot.pause()
            renderer.approval_request(
                ToolApprovalRequested(
                    call_id="call-1",
                    name="write",
                    arguments={"path": "file.txt", "content": "updated"},
                    safety="mutating",
                )
            )
            await pilot.pause()
            panel = app.query_one("#decision-panel", DecisionPanel)
            return indicator.is_open, panel.is_open

    indicator_open, decision_open = anyio.run(scenario)
    assert indicator_open is False
    assert decision_open is True


def test_session_operation_indicator_ignores_stale_completion() -> None:
    async def scenario() -> tuple[str, bool, bool, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            indicator = app.query_one("#operation-indicator", OperationIndicator)
            label = app.query_one("#operation-indicator-label", Label)
            editor.value = "draft during operation replacement"

            renderer.session_catalog_started()
            renderer.session_switch_started("target")
            renderer.session_catalog_finished()
            await pilot.pause()
            content = label.render()
            assert isinstance(content, Content)
            visible_after_stale_finish = indicator.is_open
            hidden_after_stale_finish = not editor.display

            renderer.session_switch_finished()
            await pilot.pause()
            return (
                content.plain,
                visible_after_stale_finish,
                hidden_after_stale_finish,
                indicator.is_open,
            )

    label, visible_after_stale_finish, hidden_after_stale_finish, visible_after_finish = anyio.run(
        scenario
    )
    assert label == "Switching session…"
    assert visible_after_stale_finish is True
    assert hidden_after_stale_finish is True
    assert visible_after_finish is False


def test_session_operation_indicator_fits_a_narrow_terminal() -> None:
    async def scenario() -> tuple[tuple[int, int, int, int], tuple[int, int], int]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(40, 12)) as pilot:
            renderer.session_switch_started("target")
            await pilot.pause()
            panel = app.query_one("#operation-indicator-panel", Widget)
            spinner = app.query_one("#operation-indicator-spinner", Widget)
            label = app.query_one("#operation-indicator-label", Widget)
            return (
                (panel.region.x, panel.region.y, panel.region.right, panel.region.bottom),
                (app.size.width, app.size.height),
                label.region.x - spinner.region.right,
            )

    (left, top, right, bottom), (width, height), spinner_label_gap = anyio.run(scenario)
    assert 0 <= left < right <= width
    assert 0 <= top < bottom <= height
    assert spinner_label_gap == 2


def test_session_operation_indicator_preserves_transcript_scroll_intent() -> None:
    async def scenario() -> tuple[tuple[float, bool], tuple[float, bool]]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            for index in range(24):
                app.write_assistant(f"transcript line {index}")
            # Let the queued tail-follow callbacks from the line mounts settle
            # before explicitly preserving a reader's scroll position.
            await pilot.pause()
            await pilot.pause()
            transcript = app.query_one("#transcript", Transcript)
            transcript.stop_following()
            transcript.scroll_to(y=4, animate=False)
            await pilot.pause()
            before_state = transcript.viewport_state()
            assert before_state == TranscriptViewportState(scroll_y=4, following=False)

            renderer.session_catalog_started()
            await pilot.pause()
            during_state = transcript.viewport_state()
            renderer.session_catalog_finished()
            await pilot.pause()
            return (
                (before_state.scroll_y, before_state.following),
                (during_state.scroll_y, during_state.following),
            )

    before_state, during_state = anyio.run(scenario)
    assert during_state == before_state


def test_session_switch_settlement_does_not_force_a_reader_to_the_tail() -> None:
    async def scenario() -> tuple[TranscriptViewportState, TranscriptViewportState]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            for index in range(24):
                app.write_assistant(f"transcript line {index}")
            await pilot.pause()
            await pilot.pause()
            transcript = app.query_one("#transcript", Transcript)
            transcript.stop_following()
            transcript.scroll_to(y=4, animate=False)
            await pilot.pause()
            before = transcript.viewport_state()

            renderer.session_switch_started("target")
            await pilot.pause()
            renderer.session_switch_finished()
            indicator = app.query_one("#operation-indicator", OperationIndicator)
            with anyio.fail_after(5):
                while indicator.is_open:
                    await pilot.pause()
            return before, transcript.viewport_state()

    before, after = anyio.run(scenario)

    assert after == before


def test_session_picker_table_renders_persisted_values_as_literal_text() -> None:
    session = RpcSessionSummary(
        session_id="literal",
        session_path=Path("/tmp/[archive]/literal.jsonl"),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        entry_count=1,
        name="[WIP] task",
    )

    async def scenario() -> list[str]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(100, 24)) as pilot:
            renderer.session_picker_request((session,), selected_session_id=None)
            await pilot.pause()
            table = app.query_one("#session-picker-table", DataTable)
            row = table.get_row_at(0)
            assert all(isinstance(cell, Content) for cell in row)
            return [cell.plain for cell in row if isinstance(cell, Content)]

    assert anyio.run(scenario) == [
        " ",
        "[WIP] task",
        "2026-01-01T00:00+00:00",
        "1",
        "/tmp/[archive]/literal.jso…",
    ]


def test_session_picker_wide_empty_catalog_does_not_select_a_session() -> None:
    async def scenario() -> tuple[int, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(100, 24)) as pilot:
            renderer.session_picker_request((), selected_session_id=None)
            await pilot.pause()
            table = app.query_one("#session-picker-table", DataTable)
            await pilot.press("enter")
            await pilot.pause()
            try:
                app._input_controller.receive_stream.receive_nowait()
            except anyio.WouldBlock:
                submitted = False
            else:
                submitted = True
            return table.row_count, submitted

    assert anyio.run(scenario) == (1, False)


def test_session_picker_renders_persisted_labels_as_plain_text() -> None:
    sessions = (
        RpcSessionSummary(
            session_id="literal",
            session_path=Path("/tmp/[archive]/literal.jsonl"),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            entry_count=1,
            name="[WIP] task",
        ),
    )

    async def scenario() -> Content:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            renderer.session_picker_request(sessions, selected_session_id=None)
            await pilot.pause()
            options = app.query_one("#session-picker-options", OptionList)
            return options.get_option_at_index(0).prompt

    prompt = anyio.run(scenario)
    assert isinstance(prompt, Content)
    assert "[WIP] task" in prompt.plain
    assert "/tmp/[archive]/literal.jsonl" in prompt.plain


def test_session_picker_selection_uses_resume_command_and_preserves_draft() -> None:
    async def scenario() -> tuple[str, str, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.value = "draft in progress"
            editor.cursor_position = 5
            renderer.session_picker_request((_session("target"),), selected_session_id=None)
            await pilot.pause()
            await pilot.press("enter")
            answer = await app.read_prompt("wisp> ")
            renderer.session_switch_finished()
            await pilot.pause()
            return answer, editor.value, editor.has_focus

    answer, draft, focused = anyio.run(scenario)
    assert answer == "/resume target"
    assert draft == "draft in progress"
    assert focused is True


def test_session_picker_selection_escape_guards_draft_before_shell_starts() -> None:
    async def scenario() -> tuple[str, str, bool, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.value = "draft during transport send"
            renderer.session_picker_request((_session("target"),), selected_session_id=None)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            picker = app.query_one("#session-picker", SessionPicker)
            await pilot.press("escape")
            await pilot.pause()
            guarded_draft = editor.value
            answer = await app.read_prompt("wisp> ")
            stayed_hidden = not editor.display and not picker.is_open
            renderer.session_switch_finished()
            await pilot.pause()
            return guarded_draft, answer, stayed_hidden, editor.has_focus

    draft, answer, stayed_hidden, focused = anyio.run(scenario)
    assert draft == "draft during transport send"
    assert answer == "/resume target"
    assert stayed_hidden is True
    assert focused is True


def test_session_picker_escape_restores_draft_without_submission() -> None:
    async def scenario() -> tuple[str, bool, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.value = "keep me"
            renderer.session_picker_request((_session("target"),), selected_session_id=None)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            picker = app.query_one("#session-picker", SessionPicker)
            return editor.value, editor.has_focus, picker.is_open

    draft, focused, open_ = anyio.run(scenario)
    assert draft == "keep me"
    assert focused is True
    assert open_ is False


def test_session_picker_escape_restores_draft_without_cancelling_agent() -> None:
    async def scenario() -> tuple[str, bool, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.value = "keep me too"
            renderer.session_picker_request((_session("target"),), selected_session_id=None)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            picker = app.query_one("#session-picker", SessionPicker)
            return editor.value, editor.has_focus, picker.is_open

    draft, focused, open_ = anyio.run(scenario)
    assert draft == "keep me too"
    assert focused is True
    assert open_ is False


def test_session_switch_escape_preserves_hidden_draft_until_finished() -> None:
    async def scenario() -> tuple[str, str, bool, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.value = "pending switch draft"
            renderer.session_switch_started("target")
            await pilot.pause()
            hidden_before = not editor.display
            await pilot.press("escape")
            await pilot.pause()
            draft_while_pending = editor.value
            still_hidden = not editor.display
            renderer.session_switch_finished()
            await pilot.pause()
            return (
                draft_while_pending,
                editor.value,
                hidden_before and still_hidden,
                editor.has_focus,
            )

    pending_draft, restored_draft, stayed_hidden, focused = anyio.run(scenario)
    assert pending_draft == "pending switch draft"
    assert restored_draft == "pending switch draft"
    assert stayed_hidden is True
    assert focused is True


def test_session_picker_empty_catalog_is_dismissible() -> None:
    async def scenario() -> tuple[int, bool, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(40, 12)) as pilot:
            renderer.session_picker_request((), selected_session_id=None)
            await pilot.pause()
            options = app.query_one("#session-picker-options", OptionList)
            disabled = options.get_option_at_index(0).disabled
            await pilot.press("escape")
            await pilot.pause()
            picker = app.query_one("#session-picker", SessionPicker)
            return options.option_count, disabled, picker.is_open

    count, disabled, open_ = anyio.run(scenario)
    assert count == 1
    assert disabled is True
    assert open_ is False


def test_session_picker_home_end_navigation_is_bounded() -> None:
    sessions = tuple(_session(f"session-{index}") for index in range(8))

    async def scenario() -> tuple[int | None, int | None]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(48, 14)) as pilot:
            renderer.session_picker_request(sessions, selected_session_id=None)
            await pilot.pause()
            options = app.query_one("#session-picker-options", OptionList)
            await pilot.press("end")
            await pilot.pause()
            last = options.highlighted
            await pilot.press("home")
            await pilot.pause()
            return last, options.highlighted

    assert anyio.run(scenario) == (7, 0)


def test_textual_session_switch_replaces_instead_of_appending_transcript() -> None:
    async def scenario() -> list[str]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(72, 20)) as pilot:
            renderer.prompt_submitted("old session prompt")
            await pilot.pause()
            renderer.replace_history_entries(
                (
                    HistoricalTranscriptMessage(
                        role="user",
                        content="selected session prompt",
                    ),
                ),
                session_label="Selected task",
            )
            await pilot.pause()
            transcript = app.query_one("#transcript", Transcript)
            return [
                child.render().plain
                for child in transcript.children
                if isinstance(child, LineMessage)
            ]

    lines = anyio.run(scenario)
    assert all("old session prompt" not in line for line in lines)
    assert any("Selected task" in line for line in lines)
    assert any("selected session prompt" in line for line in lines)
