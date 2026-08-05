from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import anyio
from textual.content import Content
from textual.widget import Widget
from textual.widgets import Label, LoadingIndicator, OptionList

from wisp.events import RpcSessionSummary, ToolApprovalRequested
from wisp.tui.history import HistoricalTranscriptMessage
from wisp.tui.textual_app import create_textual_tui
from wisp.tui.widgets import (
    DecisionPanel,
    LineMessage,
    OperationIndicator,
    PromptEditor,
    SessionPicker,
    Transcript,
)


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
            await pilot.press("enter", "ctrl+c")
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
    async def scenario() -> tuple[tuple[int, int, int, int], tuple[int, int]]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(40, 12)) as pilot:
            renderer.session_switch_started("target")
            await pilot.pause()
            panel = app.query_one("#operation-indicator-panel", Widget)
            return (
                (panel.region.x, panel.region.y, panel.region.right, panel.region.bottom),
                (app.size.width, app.size.height),
            )

    (left, top, right, bottom), (width, height) = anyio.run(scenario)
    assert 0 <= left < right <= width
    assert 0 <= top < bottom <= height


def test_session_operation_indicator_preserves_transcript_scroll_intent() -> None:
    async def scenario() -> tuple[tuple[float, bool], tuple[float, bool]]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            for index in range(24):
                app.write_assistant(f"transcript line {index}")
            await pilot.pause()
            transcript = app.query_one("#transcript", Transcript)
            transcript.scroll_to(y=4, animate=False)
            await pilot.pause()
            before_state = transcript.viewport_state()

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


def test_session_picker_selection_guards_draft_before_shell_starts() -> None:
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
            await pilot.press("ctrl+c")
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


def test_session_picker_ctrl_c_restores_draft_without_interrupting() -> None:
    async def scenario() -> tuple[str, bool, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.value = "keep me too"
            renderer.session_picker_request((_session("target"),), selected_session_id=None)
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.pause()
            picker = app.query_one("#session-picker", SessionPicker)
            return editor.value, editor.has_focus, picker.is_open

    draft, focused, open_ = anyio.run(scenario)
    assert draft == "keep me too"
    assert focused is True
    assert open_ is False


def test_session_switch_ctrl_c_preserves_hidden_draft_until_finished() -> None:
    async def scenario() -> tuple[str, str, bool, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.value = "pending switch draft"
            renderer.session_switch_started("target")
            await pilot.pause()
            hidden_before = not editor.display
            await pilot.press("ctrl+c")
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
