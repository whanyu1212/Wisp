"""Centered Textual prompt for an available Wisp update."""

from __future__ import annotations

import time

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option

from wisp.tui.update_types import UpdatePromptAction
from wisp.update_check import UpdateAvailable


class UpdatePrompt(Vertical):
    """Modal update choice with a deliberate update-first default."""

    BINDING_GROUP_TITLE = "Wisp update"
    BINDINGS = [
        Binding("1", "choose(1)", "Update and restart", show=False),
        Binding("2", "choose(2)", "Later", show=False),
        Binding("3", "choose(3)", "Skip version", show=False),
        Binding("escape", "later", "Later", show=False),
    ]

    DEFAULT_CSS = """
    UpdatePrompt {
        overlay: screen;
        display: none;
        width: 100%;
        height: 100%;
        align: center middle;
        background: $background 75%;
    }

    UpdatePrompt #update-prompt-panel {
        width: 64;
        max-width: 92%;
        height: auto;
        max-height: 13;
        padding: 1 2;
        border: heavy $accent;
        background: $panel;
    }

    UpdatePrompt #update-prompt-title {
        height: 1;
        color: $accent;
        text-style: bold;
    }

    UpdatePrompt #update-prompt-detail {
        height: auto;
        margin: 1 0;
        color: $text;
    }

    UpdatePrompt #update-prompt-options {
        height: 3;
        border: none;
        background: transparent;
        padding: 0;
        scrollbar-size: 0 0;
    }

    UpdatePrompt #update-prompt-options > .option-list--option-highlighted {
        background: $accent 30%;
    }
    """

    class Selected(Message):
        """A user-selected update action and its immutable release offer."""

        def __init__(self, action: UpdatePromptAction, update: UpdateAvailable) -> None:
            super().__init__()
            self.action = action
            self.update = update

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 - Textual API
        super().__init__(id=id)
        self._title = Label("Update available", id="update-prompt-title")
        self._detail = Static("", id="update-prompt-detail", markup=False)
        self._options = OptionList(id="update-prompt-options")
        self._update: UpdateAvailable | None = None
        self._submitted = False
        self._opened_at = 0.0

    def compose(self) -> ComposeResult:
        with Vertical(id="update-prompt-panel"):
            yield self._title
            yield self._detail
            yield self._options

    @property
    def is_open(self) -> bool:
        return self.display

    def show_update(self, update: UpdateAvailable) -> None:
        self._update = update
        self._submitted = False
        self._opened_at = time.monotonic()
        self._detail.update(
            f"Wisp {update.latest_version} is available. "
            f"You are currently running {update.current_version}."
        )
        self._options.clear_options()
        self._options.add_options(
            [
                Option("1  Update & restart (default)", id="update_and_restart"),
                Option("2  Later", id="later"),
                Option(f"3  Skip {update.latest_version}", id="skip_version"),
            ]
        )
        self._options.highlighted = 0
        self.display = True
        self._options.focus()

    def hide(self) -> None:
        self.display = False
        self._submitted = False

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list is not self._options:
            return
        event.stop()
        if event.time < self._opened_at or event.option.id is None:
            return
        try:
            action = UpdatePromptAction(event.option.id)
        except ValueError:
            return
        self._submit(action)

    def on_key(self, event: events.Key) -> None:
        if self.is_open and event.time < self._opened_at:
            event.stop()
            event.prevent_default()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action != "choose" or not parameters or not isinstance(parameters[0], int):
            return True
        return 1 <= parameters[0] <= 3

    def action_choose(self, number: int) -> None:
        actions = {
            1: UpdatePromptAction.update_and_restart,
            2: UpdatePromptAction.later,
            3: UpdatePromptAction.skip_version,
        }
        if action := actions.get(number):
            self._submit(action)

    def action_later(self) -> None:
        self._submit(UpdatePromptAction.later)

    def _submit(self, action: UpdatePromptAction) -> None:
        update = self._update
        if self._submitted or not self.is_open or update is None:
            return
        self._submitted = True
        self.post_message(self.Selected(action, update))


__all__ = ["UpdatePrompt"]
