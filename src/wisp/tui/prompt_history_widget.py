"""Searchable Textual overlay for process-local prompt history."""

from __future__ import annotations

import time

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content
from textual.message import Message
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from wisp.tui.prompt_history import PromptHistoryEntry, search_prompt_history


class PromptHistoryPicker(Vertical):
    """Search recent prompts and restore one to the editor without submitting."""

    BINDING_GROUP_TITLE = "Prompt history"
    HELP = """
    # Prompt history

    Type to filter prompts submitted during this TUI process. Navigation changes
    the highlight; Enter restores the selected prompt to the editor for review and
    never submits it. Escape closes history without changing the draft.
    """
    BINDINGS = [
        Binding(
            "up", "move('action_cursor_up')", "Previous prompt", show=False, priority=True
        ),
        Binding("down", "move('action_cursor_down')", "Next prompt", show=False, priority=True),
        Binding("pageup", "move('action_page_up')", "Previous page", show=False, priority=True),
        Binding("pagedown", "move('action_page_down')", "Next page", show=False, priority=True),
        Binding("home", "move('action_first')", "First prompt", show=False, priority=True),
        Binding("end", "move('action_last')", "Last prompt", show=False, priority=True),
        Binding("enter", "restore", "Restore prompt", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    PromptHistoryPicker {
        overlay: screen;
        constrain: inside;
        display: none;
        width: 72;
        max-width: 90%;
        height: auto;
        max-height: 18;
        offset: 0 -100%;
        border: round $accent;
        background: $panel;
        padding: 0 1;
    }

    PromptHistoryPicker #prompt-history-query {
        height: 3;
        border: none;
        border-bottom: solid $secondary;
        background: transparent;
        padding: 0 1;
    }

    PromptHistoryPicker #prompt-history-options {
        height: auto;
        max-height: 12;
        border: none;
        background: transparent;
        padding: 0;
        scrollbar-size-vertical: 1;
    }

    PromptHistoryPicker #prompt-history-options > .option-list--option-highlighted {
        background: transparent;
        color: $accent;
    }

    PromptHistoryPicker #prompt-history-hint {
        height: 1;
        color: $text-muted;
        text-align: right;
        text-style: dim;
    }
    """

    class Selected(Message):
        """One exact prompt explicitly selected for editor restoration."""

        def __init__(self, prompt: str) -> None:
            super().__init__()
            self.prompt = prompt

    class Cancelled(Message):
        """The picker was dismissed without changing the editor."""

    def __init__(self, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id)
        self._query = Input(placeholder="Search recent prompts", id="prompt-history-query")
        self._options = OptionList(id="prompt-history-options")
        self._hint = Static(
            "↑↓ navigate · enter restore · esc cancel",
            id="prompt-history-hint",
            markup=False,
        )
        self._entries: tuple[PromptHistoryEntry, ...] = ()
        self._visible: tuple[PromptHistoryEntry, ...] = ()
        self._submitted = False
        self._opened_at = 0.0

    def compose(self) -> ComposeResult:
        yield self._query
        yield self._options
        yield self._hint

    @property
    def is_open(self) -> bool:
        return self.display

    def move_highlight_page_up(self) -> None:
        self._options.action_page_up()  # type: ignore[no-untyped-call]

    def move_highlight_page_down(self) -> None:
        self._options.action_page_down()  # type: ignore[no-untyped-call]

    def move_highlight_first(self) -> None:
        self._options.action_first()

    def move_highlight_last(self) -> None:
        self._options.action_last()

    def show(self, entries: tuple[PromptHistoryEntry, ...]) -> None:
        self._entries = entries
        self._submitted = False
        self._opened_at = time.monotonic()
        self._query.value = ""
        self.display = True
        self._refresh_options()
        self._query.focus()

    def hide(self) -> None:
        self.display = False
        self._submitted = False
        self._entries = ()
        self._visible = ()
        self._options.clear_options()

    def _refresh_options(self) -> None:
        self._visible = search_prompt_history(self._entries, self._query.value)
        self._options.clear_options()
        for entry in self._visible:
            self._options.add_option(Option(Content(entry.preview), id=str(entry.sequence)))
        if self._visible:
            self._options.highlighted = 0
            self._hint.update("↑↓ navigate · enter restore · esc cancel")
        else:
            message = (
                "No prompts submitted in this TUI run."
                if not self._entries
                else "No matching prompts."
            )
            self._options.add_option(Option(message, disabled=True))
            self._hint.update("esc close")

    def _move(self, action: str) -> None:
        getattr(self._options, action)()

    def submit_current_selection(self) -> None:
        if self._submitted or not self.is_open:
            return
        highlighted = self._options.highlighted
        if highlighted is None or highlighted >= len(self._visible):
            return
        self._submitted = True
        self.post_message(self.Selected(self._visible[highlighted].prompt))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input is not self._query:
            return
        event.stop()
        self._refresh_options()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list is not self._options:
            return
        event.stop()
        if event.time < self._opened_at:
            return
        self.submit_current_selection()

    def on_key(self, event: events.Key) -> None:
        if not self.is_open:
            return
        if event.time < self._opened_at:
            event.prevent_default()
            event.stop()
            return
        actions = {
            "down": "action_cursor_down",
            "up": "action_cursor_up",
            "pagedown": "action_page_down",
            "pageup": "action_page_up",
            "home": "action_first",
            "end": "action_last",
        }
        action = actions.get(event.key)
        if action is not None:
            self.action_move(action)
            event.prevent_default()
            event.stop()
        elif event.key == "enter":
            self.action_restore()
            event.prevent_default()
            event.stop()
        elif event.key == "escape":
            self.action_cancel()
            event.prevent_default()
            event.stop()

    def action_move(self, action: str) -> None:
        self._move(action)

    def action_restore(self) -> None:
        self.submit_current_selection()

    def action_cancel(self) -> None:
        self.post_message(self.Cancelled())


__all__ = ["PromptHistoryPicker"]
