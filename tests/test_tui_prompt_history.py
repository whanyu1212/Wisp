from __future__ import annotations

import anyio
import pytest
from textual.content import Content
from textual.widgets import Input, OptionList

from tests.tui_support import ScriptedController
from wisp.events import ToolApprovalRequested
from wisp.tui import TuiViewSnapshot
from wisp.tui.overlay import TranscriptViewportState
from wisp.tui.prompt_history import (
    PROMPT_HISTORY_PREVIEW_CHARS,
    PROMPT_HISTORY_SEARCH_CHARS,
    PromptHistory,
)
from wisp.tui.prompt_history_widget import PromptHistoryPicker
from wisp.tui.shell import TuiShell
from wisp.tui.state import _InputLine, _InputMode
from wisp.tui.textual_app import TextualTui, create_textual_tui
from wisp.tui.widgets import PASTE_DISPLAY_THRESHOLD, PromptEditor, Transcript

pytestmark = pytest.mark.tui


def test_prompt_history_is_bounded_unique_mru_with_exact_text() -> None:
    history = PromptHistory(capacity=3)

    history.record("first")
    history.record("second\nline")
    history.record("third")
    moved = history.record("first")
    history.record("fourth")

    assert moved is not None
    assert tuple(entry.prompt for entry in history.entries) == (
        "fourth",
        "first",
        "third",
    )
    assert history.entries[1].prompt == "first"


def test_prompt_history_ignores_blank_prompts_and_validates_capacity() -> None:
    history = PromptHistory()

    assert history.record(" \n\t") is None
    assert history.entries == ()
    with pytest.raises(ValueError, match="capacity must be positive"):
        PromptHistory(capacity=0)


def test_prompt_history_search_is_casefolded_whitespace_normalized_and_deterministic() -> None:
    history = PromptHistory()
    history.record("older ALPHA\n beta prompt")
    history.record("newest alpha   beta prompt")
    history.record("unrelated")

    assert tuple(entry.prompt for entry in history.search("  Alpha BETA ")) == (
        "newest alpha   beta prompt",
        "older ALPHA\n beta prompt",
    )
    assert history.search("") == history.entries


def test_prompt_history_preview_is_bounded_without_truncating_restored_prompt() -> None:
    prompt = "[bold]" + "é" * (PROMPT_HISTORY_PREVIEW_CHARS + 20) + "\nlast line"
    history = PromptHistory()

    entry = history.record(prompt)

    assert entry is not None
    assert len(entry.preview) == PROMPT_HISTORY_PREVIEW_CHARS
    assert entry.preview.endswith("…")
    assert entry.prompt == prompt


def test_prompt_history_caches_a_bounded_search_prefix_without_rescanning_prompt() -> None:
    class SplitOncePrompt(str):
        split_calls = 0

        def split(self, sep: str | None = None, maxsplit: int = -1) -> list[str]:
            self.split_calls += 1
            if self.split_calls > 1:
                raise AssertionError("stored prompt was rescanned")
            return super().split(sep, maxsplit)

    prefix = "a" * PROMPT_HISTORY_SEARCH_CHARS
    prompt = SplitOncePrompt(f"{prefix} needle beyond bounded index")
    history = PromptHistory()

    entry = history.record(prompt)

    assert entry is not None
    assert entry.prompt == prompt
    assert len(entry.search_text) == PROMPT_HISTORY_SEARCH_CHARS
    assert history.search("aaa") == (entry,)
    assert history.search("needle") == ()
    assert prompt.split_calls == 0


def test_ctrl_r_cancel_preserves_draft_selection_and_focus() -> None:
    async def scenario() -> tuple[str, object, bool, bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.value = "draft in progress"
            editor.selection = type(editor.selection)((0, 2), (0, 8))
            await pilot.press("ctrl+r")
            await pilot.pause()
            picker = app.query_one("#prompt-history", PromptHistoryPicker)
            opened = picker.is_open
            await pilot.press("escape")
            await pilot.pause()
            return editor.value, editor.selection, opened, editor.has_focus

    draft, selection, opened, focused = anyio.run(scenario)
    assert draft == "draft in progress"
    assert selection.start == (0, 2)
    assert selection.end == (0, 8)
    assert opened is True
    assert focused is True


def test_escape_closes_history_instead_of_signalling_cancel() -> None:
    async def scenario() -> tuple[bool, bool, str]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.value = "keep me"
            app.record_prompt("remember me")
            await pilot.press("ctrl+r")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            picker = app.query_one("#prompt-history", PromptHistoryPicker)
            with pytest.raises(anyio.WouldBlock):
                app._input_controller.receive_stream.receive_nowait()
            return picker.is_open, editor.has_focus, editor.value

    opened, focused, draft = anyio.run(scenario)
    assert not opened
    assert focused
    assert draft == "keep me"


def test_history_close_releases_exact_prompt_snapshot() -> None:
    async def scenario() -> tuple[
        tuple[object, ...],
        tuple[object, ...],
        int,
    ]:
        app = TextualTui()
        exact_prompt = "sensitive " + "x" * (PASTE_DISPLAY_THRESHOLD + 1)
        async with app.run_test(size=(80, 24)) as pilot:
            app.record_prompt(exact_prompt)
            await pilot.press("ctrl+r")
            await pilot.pause()
            picker = app.query_one("#prompt-history", PromptHistoryPicker)
            assert picker._entries[0].prompt == exact_prompt
            await pilot.press("escape")
            await pilot.pause()
            return picker._entries, picker._visible, picker._options.option_count

    entries, visible, option_count = anyio.run(scenario)
    assert entries == ()
    assert visible == ()
    assert option_count == 0


def test_history_search_restores_exact_prompt_without_submitting() -> None:
    async def scenario() -> tuple[str, bool, str]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            app.record_prompt("older prompt")
            app.record_prompt("Exact [bold]\nmultiline prompt")
            editor = app.query_one("#input", PromptEditor)
            editor.value = "draft replaced by explicit selection"
            await pilot.press("ctrl+r")
            await pilot.pause()
            query = app.query_one("#prompt-history-query", Input)
            query.value = "MULTILINE"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            with pytest.raises(anyio.WouldBlock):
                app._input_controller.receive_stream.receive_nowait()
            restored = editor.value
            focused = editor.has_focus
            await pilot.press("enter")
            submitted = await app.read_prompt("wisp> ")
            return restored, focused, submitted

    restored, focused, submitted = anyio.run(scenario)
    assert restored == "Exact [bold]\nmultiline prompt"
    assert focused
    assert submitted == restored


def test_history_selection_clears_stale_large_paste_backing() -> None:
    async def scenario() -> tuple[str, str]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor._show_large_paste_placeholder("secret backing" * 500)
            marker = editor.value
            app.record_prompt(marker)
            await pilot.press("ctrl+r")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            return editor.value, editor.text_for_submission()

    restored, submitted = anyio.run(scenario)
    assert restored.startswith("[Pasted content #1:")
    assert submitted == restored


def test_history_selection_keeps_large_prompt_compact_and_submits_exact_text() -> None:
    async def scenario() -> tuple[str, str, str]:
        app = TextualTui()
        prompt = "large history prompt\n" + "é" * (PASTE_DISPLAY_THRESHOLD + 1)
        async with app.run_test(size=(80, 24)) as pilot:
            app.record_prompt(prompt)
            await pilot.press("ctrl+r")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            editor = app.query_one("#input", PromptEditor)
            display = editor.value
            expanded = editor.text_for_submission()
            await pilot.press("enter")
            submitted = await app.read_prompt("wisp> ")
            return display, expanded, submitted

    display, expanded, submitted = anyio.run(scenario)
    assert display.startswith("[Pasted content #1:")
    assert len(display) < PASTE_DISPLAY_THRESHOLD
    assert expanded == submitted
    assert submitted.startswith("large history prompt\n")
    assert len(submitted) > PASTE_DISPLAY_THRESHOLD


def test_history_filter_renders_literal_content_and_empty_states() -> None:
    async def scenario() -> tuple[str, str, int, Content]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+r")
            await pilot.pause()
            options = app.query_one("#prompt-history-options", OptionList)
            empty = options.get_option_at_index(0).prompt
            await pilot.press("escape")
            app.record_prompt("[red]literal[/red] alpha")
            app.record_prompt("beta")
            await pilot.press("ctrl+r")
            await pilot.pause()
            query = app.query_one("#prompt-history-query", Input)
            query.value = "alpha"
            await pilot.pause()
            prompt = options.get_option_at_index(0).prompt
            return str(empty), prompt.plain, options.option_count, prompt

    empty, prompt_text, option_count, prompt = anyio.run(scenario)
    assert "No prompts submitted" in empty
    assert option_count == 1
    assert prompt_text == "[red]literal[/red] alpha"
    assert isinstance(prompt, Content)


def test_history_mouse_selection_restores_prompt() -> None:
    async def scenario() -> tuple[bool, str]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            app.record_prompt("restore by mouse")
            await pilot.press("ctrl+r")
            await pilot.pause()
            clicked = await pilot.click("#prompt-history-options", offset=(2, 0))
            await pilot.pause()
            editor = app.query_one("#input", PromptEditor)
            return clicked, editor.value

    clicked, restored = anyio.run(scenario)
    assert clicked
    assert restored == "restore by mouse"


def test_history_cannot_displace_an_approval_overlay() -> None:
    async def scenario() -> tuple[bool, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            app.record_prompt("remember me")
            renderer.view_updated(
                TuiViewSnapshot(
                    status="waiting for approval",
                    input_hint="approve> ",
                    input_mode="approval",
                    cwd="/work/project",
                )
            )
            renderer.approval_request(
                ToolApprovalRequested(
                    call_id="call-1",
                    name="write",
                    arguments={"path": "file.txt", "content": "content"},
                    safety="mutating",
                )
            )
            await pilot.pause()
            await pilot.press("ctrl+r")
            await pilot.pause()
            return (
                app.query_one("#decision-panel").display,
                app.query_one("#prompt-history", PromptHistoryPicker).is_open,
            )

    decision_open, history_open = anyio.run(scenario)
    assert decision_open
    assert not history_open


def test_displaced_history_selection_cannot_overwrite_or_focus_hidden_draft() -> None:
    async def scenario() -> tuple[str, bool, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            app.record_prompt("history replacement")
            editor = app.query_one("#input", PromptEditor)
            editor.value = "draft must survive"
            await pilot.press("ctrl+r")
            await pilot.pause()
            stale_selection = PromptHistoryPicker.Selected("history replacement")

            renderer.approval_request(
                ToolApprovalRequested(
                    call_id="call-1",
                    name="write",
                    arguments={"path": "file.txt", "content": "content"},
                    safety="mutating",
                )
            )
            await pilot.pause()
            app.on_prompt_history_picker_selected(stale_selection)
            await pilot.pause()

            return (
                editor.value,
                editor.has_focus,
                app.query_one("#decision-panel").display,
            )

    draft, editor_focused, decision_open = anyio.run(scenario)
    assert draft == "draft must survive"
    assert not editor_focused
    assert decision_open


def test_history_rejects_selection_event_timestamped_before_open() -> None:
    async def scenario() -> tuple[bool, str]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            app.record_prompt("must not restore")
            editor = app.query_one("#input", PromptEditor)
            editor.value = "keep draft"
            await pilot.press("ctrl+r")
            await pilot.pause()
            picker = app.query_one("#prompt-history", PromptHistoryPicker)
            options = app.query_one("#prompt-history-options", OptionList)
            selected = OptionList.OptionSelected(
                options,
                options.get_option_at_index(0),
                0,
            )
            selected.time = picker._opened_at - 1.0
            picker.on_option_list_option_selected(selected)
            await pilot.pause()
            return picker.is_open, editor.value

    open_, draft = anyio.run(scenario)
    assert open_
    assert draft == "keep draft"


def test_history_cancel_preserves_transcript_viewport() -> None:
    async def scenario() -> tuple[float, float, bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            app.record_prompt("remember me")
            for index in range(40):
                app.write_notice(f"historical line {index}")
            await pilot.pause()
            transcript = app.query_one("#transcript", Transcript)
            transcript.restore_viewport_state(
                TranscriptViewportState(scroll_y=0.0, following=False)
            )
            await pilot.pause()
            before = transcript.scroll_y
            await pilot.press("ctrl+r")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            return before, transcript.scroll_y, transcript.is_following

    before, after, following = anyio.run(scenario)
    assert before == after
    assert not following


@pytest.mark.parametrize("size", [(40, 16), (80, 24), (120, 40)])
def test_history_overlay_is_bounded_above_composer(size: tuple[int, int]) -> None:
    async def scenario() -> tuple[int, int, int]:
        app = TextualTui()
        async with app.run_test(size=size) as pilot:
            app.record_prompt("remember me")
            editor_top = app.query_one("#input").region.y
            await pilot.press("ctrl+r")
            await pilot.pause()
            picker = app.query_one("#prompt-history", PromptHistoryPicker)
            return picker.region.x, picker.region.right, picker.region.bottom - editor_top

    left, right, bottom_delta = anyio.run(scenario)
    assert left >= 0
    assert right <= size[0]
    assert bottom_delta <= 0


def test_textual_renderer_records_only_explicit_prompt_submission_seam() -> None:
    app, renderer = create_textual_tui()

    renderer.render_history(())
    renderer.prompt_history_request()
    renderer.prompt_submitted("echoed only when execution starts")
    assert app._input_controller.prompt_history_entries == ()

    renderer.prompt_accepted("real submitted prompt")

    assert tuple(entry.prompt for entry in app._input_controller.prompt_history_entries) == (
        "real submitted prompt",
    )


def test_shell_records_real_prompts_but_not_history_or_help_commands() -> None:
    async def scenario() -> tuple[str, ...]:
        app, renderer = create_textual_tui()
        shell = TuiShell(ScriptedController(), renderer=renderer)

        await shell._handle_input_line(_InputLine("/history", _InputMode.idle))
        await shell._handle_input_line(_InputLine("/help", _InputMode.idle))
        await shell._handle_input_line(_InputLine("real prompt", _InputMode.idle))

        return tuple(entry.prompt for entry in app._input_controller.prompt_history_entries)

    assert anyio.run(scenario) == ("real prompt",)


def test_shell_accepts_prompts_with_legacy_renderer_without_history_hook() -> None:
    class LegacyRenderer:
        def __init__(self, delegate: object) -> None:
            self.delegate = delegate

        def __getattr__(self, name: str) -> object:
            if name == "prompt_accepted":
                raise AttributeError(name)
            return getattr(self.delegate, name)

    async def scenario() -> list[str]:
        app, renderer = create_textual_tui()
        controller = ScriptedController()
        shell = TuiShell(controller, renderer=LegacyRenderer(renderer))  # type: ignore[arg-type]

        await shell._handle_input_line(_InputLine("compatible prompt", _InputMode.idle))
        return controller.prompts

    assert anyio.run(scenario) == ["compatible prompt"]


def test_shell_records_queued_prompt_immediately_and_queue_clear_does_not_erase_it() -> None:
    async def scenario() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        app, renderer = create_textual_tui()
        shell = TuiShell(ScriptedController(), renderer=renderer)
        shell.state.current_command_id = "prompt-1"
        shell.state.current_command_type = "prompt"

        await shell._handle_input_line(_InputLine("submitted follow-up", _InputMode.running))
        recorded_before_clear = tuple(
            entry.prompt for entry in app._input_controller.prompt_history_entries
        )
        queued_before_clear = tuple(shell.state.queued_prompts)
        shell._clear_queued_prompts()
        recorded_after_clear = tuple(
            entry.prompt for entry in app._input_controller.prompt_history_entries
        )
        return recorded_before_clear, queued_before_clear, recorded_after_clear

    before, queued, after = anyio.run(scenario)
    assert before == ("submitted follow-up",)
    assert queued == ("submitted follow-up",)
    assert after == before
