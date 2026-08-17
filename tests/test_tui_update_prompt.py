from __future__ import annotations

import anyio
import pytest
from textual.widgets import OptionList

from wisp.tui import TuiViewSnapshot
from wisp.tui.state import TuiCancelRequested
from wisp.tui.textual_app import create_textual_tui
from wisp.tui.update_prompt import UpdatePrompt
from wisp.tui.update_types import UpdatePromptAction
from wisp.tui.widgets import OperationIndicator, PromptEditor
from wisp.update_check import UpdateAvailable

pytestmark = pytest.mark.tui


def _update() -> UpdateAvailable:
    return UpdateAvailable("1.0.0", "1.1.0", "wisp update")


def test_update_prompt_waits_for_an_empty_idle_composer() -> None:
    async def scenario() -> tuple[bool, bool, int | None]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            prompt = app.query_one("#update-prompt", UpdatePrompt)
            editor.value = "unfinished draft"
            renderer.update_available(_update(), automatic_install=True)
            await pilot.pause()
            hidden_with_draft = not prompt.is_open

            editor.value = ""
            await pilot.pause()
            await pilot.pause()
            options = prompt.query_one("#update-prompt-options", OptionList)
            return hidden_with_draft, prompt.is_open, options.highlighted

    hidden_with_draft, visible_when_safe, highlighted = anyio.run(scenario)
    assert hidden_with_draft
    assert visible_when_safe
    assert highlighted == 0


def test_update_prompt_defers_while_a_turn_is_running() -> None:
    async def scenario() -> tuple[bool, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            prompt = app.query_one("#update-prompt", UpdatePrompt)
            renderer.view_updated(
                TuiViewSnapshot(
                    status="running",
                    input_hint="wisp(running)> ",
                    input_mode="running",
                )
            )
            renderer.update_available(_update(), automatic_install=True)
            await pilot.pause()
            hidden_while_running = not prompt.is_open
            renderer.view_updated(
                TuiViewSnapshot(status="idle", input_hint="wisp> ", input_mode="idle")
            )
            await pilot.pause()
            await pilot.pause()
            return hidden_while_running, prompt.is_open

    hidden_while_running, visible_when_idle = anyio.run(scenario)
    assert hidden_while_running
    assert visible_when_idle


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        (("enter",), UpdatePromptAction.update_and_restart),
        (("3",), UpdatePromptAction.skip_version),
    ],
)
def test_update_prompt_dispatches_explicit_actions(
    keys: tuple[str, ...],
    expected: UpdatePromptAction,
) -> None:
    async def scenario() -> list[UpdatePromptAction]:
        app, renderer = create_textual_tui()
        selected: list[UpdatePromptAction] = []

        async def handle(action: UpdatePromptAction, _update: UpdateAvailable) -> None:
            selected.append(action)

        renderer.set_update_action_hook(handle)
        async with app.run_test(size=(80, 24)) as pilot:
            renderer.update_available(_update(), automatic_install=True)
            await pilot.pause()
            await pilot.press(*keys)
            await pilot.pause()
        return selected

    assert anyio.run(scenario) == [expected]


def test_update_prompt_escape_means_later_without_invoking_the_shell() -> None:
    async def scenario() -> tuple[bool, list[UpdatePromptAction]]:
        app, renderer = create_textual_tui()
        selected: list[UpdatePromptAction] = []

        async def handle(action: UpdatePromptAction, _update: UpdateAvailable) -> None:
            selected.append(action)

        renderer.set_update_action_hook(handle)
        async with app.run_test(size=(80, 24)) as pilot:
            renderer.update_available(_update(), automatic_install=True)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            return app.query_one("#update-prompt", UpdatePrompt).is_open, selected

    visible, selected = anyio.run(scenario)
    assert not visible
    assert selected == []


def test_update_operation_escape_reaches_shell_cancellation_and_restores_composer() -> None:
    async def scenario() -> tuple[bool, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            renderer.update_operation_started(_update())
            await pilot.pause()
            indicator_open = app.query_one("#operation-indicator", OperationIndicator).is_open
            await pilot.press("escape")
            with pytest.raises(TuiCancelRequested):
                await app.read_prompt("wisp> ")
            renderer.update_operation_finished(installed=False, restarting=False)
            await pilot.pause()
            return indicator_open, app.query_one("#input", PromptEditor).display

    indicator_open, composer_visible = anyio.run(scenario)
    assert indicator_open
    assert composer_visible
