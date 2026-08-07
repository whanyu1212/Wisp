from __future__ import annotations

import anyio
import pytest
from textual.widgets import HelpPanel, Markdown

from wisp.events import ToolApprovalRequested
from wisp.tui.textual_app import create_textual_tui
from wisp.tui.widgets import DecisionPanel, PromptEditor, ToolCard, Transcript

pytestmark = pytest.mark.tui


def _help_source(app: object) -> str:
    return app.query_one("#widget-help", Markdown).source  # type: ignore[attr-defined]


def test_ctrl_g_toggles_native_help_without_moving_focus_or_draft() -> None:
    async def scenario() -> None:
        app, _renderer = create_textual_tui()
        async with app.run_test(size=(100, 30)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.text = "draft prompt"
            assert app.screen.focused is editor

            await pilot.press("ctrl+g")
            await pilot.pause()

            assert len(app.query(HelpPanel)) == 1
            assert app.screen.focused is editor
            assert editor.text == "draft prompt"
            assert "# Prompt editor" in _help_source(app)

            await pilot.press("ctrl+g")
            await pilot.pause()

            assert not app.query(HelpPanel)
            assert app.screen.focused is editor
            assert editor.text == "draft prompt"

    anyio.run(scenario)


def test_closing_help_preserves_navigation_performed_while_open() -> None:
    async def scenario() -> None:
        app, _renderer = create_textual_tui()
        async with app.run_test() as pilot:
            transcript = app.query_one("#transcript", Transcript)
            assert transcript.is_following

            await pilot.press("ctrl+g")
            await pilot.pause()
            transcript.stop_following()
            assert not transcript.is_following

            await pilot.press("ctrl+g")
            await pilot.pause()

            assert not transcript.is_following

    anyio.run(scenario)


def test_textual_help_command_uses_the_same_contextual_panel() -> None:
    async def scenario() -> None:
        app, renderer = create_textual_tui()
        async with app.run_test() as pilot:
            renderer.help()
            await pilot.pause()
            assert len(app.query(HelpPanel)) == 1

            renderer.help()
            await pilot.pause()
            assert not app.query(HelpPanel)

    anyio.run(scenario)


def test_help_tracks_tool_card_focus_and_hides_unavailable_expand_binding() -> None:
    async def scenario() -> None:
        app, _renderer = create_textual_tui()
        async with app.run_test() as pilot:
            app.action_toggle_contextual_help()
            await pilot.pause()
            assert "# Prompt editor" in _help_source(app)

            transcript = app.query_one("#transcript", Transcript)
            card = ToolCard("read", {"path": "README.md"})
            card.set_state("done", detail="complete", full_output="complete")
            await transcript.mount_message(card)
            card.focus()
            await pilot.pause()

            assert "# Tool result" in _help_source(app)
            actions = {binding.binding.action for binding in app.screen.active_bindings.values()}
            assert "toggle_expand" not in actions

            card.set_state("done", detail="preview", full_output="preview\nmore")
            await pilot.pause()
            actions = {binding.binding.action for binding in app.screen.active_bindings.values()}
            assert "toggle_expand" in actions

    anyio.run(scenario)


def test_help_tracks_safety_panel_and_exposes_only_available_decisions() -> None:
    async def scenario() -> None:
        app, _renderer = create_textual_tui()
        async with app.run_test() as pilot:
            app.show_approval(
                ToolApprovalRequested(
                    call_id="call-1",
                    name="bash",
                    arguments={"command": "uv run pytest"},
                    safety="command",
                ),
                cwd="/work/project",
            )
            app.action_toggle_contextual_help()
            await pilot.pause()

            assert isinstance(app.screen.focused.parent, DecisionPanel)
            source = _help_source(app)
            assert "# Safety decision" in source
            assert "Approve once" in source
            assert "until this Wisp process exits" in source
            actions = {binding.binding.action for binding in app.screen.active_bindings.values()}
            assert {"choose(1)", "choose(2)", "choose(3)", "choose(4)"} <= actions

    anyio.run(scenario)


def test_suggestion_bindings_appear_only_while_a_menu_is_open() -> None:
    async def scenario() -> None:
        app, _renderer = create_textual_tui()
        async with app.run_test() as pilot:
            editor = app.query_one("#input", PromptEditor)
            app.action_toggle_contextual_help()
            editor.text = "/"
            await pilot.pause()

            actions = {binding.binding.action for binding in app.screen.active_bindings.values()}
            assert "menu_move(-1)" in actions
            assert "menu_move(1)" in actions
            assert "menu_complete" in actions

            editor.text = "ordinary prompt"
            await pilot.pause()
            actions = {binding.binding.action for binding in app.screen.active_bindings.values()}
            assert not any(action.startswith("menu_") for action in actions)

    anyio.run(scenario)


@pytest.mark.parametrize(
    ("size", "compact"),
    [((100, 30), False), ((60, 20), True)],
)
@pytest.mark.parametrize("theme", ["wisp", "wisp-light"])
def test_help_uses_responsive_split(size: tuple[int, int], compact: bool, theme: str) -> None:
    async def scenario() -> None:
        app, _renderer = create_textual_tui()
        async with app.run_test(size=size) as pilot:
            app.theme = theme
            await pilot.press("ctrl+g")
            await pilot.pause()
            panel = app.query_one(HelpPanel)

            assert app.screen.has_class("-compact-help") is compact
            if compact:
                assert panel.region.width == app.screen.size.width
                assert panel.region.y > 0
            else:
                assert panel.region.width < app.screen.size.width
                assert panel.region.x > 0

    anyio.run(scenario)


def test_help_content_is_static_and_does_not_interpolate_tool_text() -> None:
    async def scenario() -> None:
        app, _renderer = create_textual_tui()
        async with app.run_test() as pilot:
            transcript = app.query_one("#transcript", Transcript)
            card = ToolCard("[link=https://example.invalid]unsafe[/link]", {})
            await transcript.mount_message(card)
            card.focus()
            await pilot.press("ctrl+g")
            await pilot.pause()

            source = _help_source(app)
            assert "# Tool result" in source
            assert "example.invalid" not in source
            assert "unsafe" not in source

    anyio.run(scenario)
