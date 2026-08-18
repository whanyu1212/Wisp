"""Curated Textual theme picker with reversible live preview."""

from __future__ import annotations

import time

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content
from textual.message import Message
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from wisp.tui.theme import WISP_THEME_SPECS, WispThemeSpec


class ThemePicker(Vertical):
    """Choose among Wisp's curated themes while previewing the highlighted row."""

    BINDING_GROUP_TITLE = "Theme picker"
    HELP = """
    # Theme picker

    Move through the curated themes to preview them immediately. Enter keeps the
    highlighted theme; Escape restores the theme that was active when opened.
    """
    BINDINGS = [
        Binding("enter", "select", "Use theme", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    ThemePicker {
        overlay: screen;
        constrain: inside;
        display: none;
        width: 56;
        max-width: 90%;
        height: auto;
        max-height: 12;
        offset: 0 -100%;
        border-left: heavy $accent;
        background: $panel;
        padding: 0 1;
    }

    ThemePicker #theme-picker-title {
        height: 1;
        color: $accent;
        text-style: bold;
    }

    ThemePicker #theme-picker-options {
        height: auto;
        max-height: 8;
        border: none;
        background: transparent;
        padding: 0;
    }

    ThemePicker #theme-picker-options > .option-list--option-highlighted {
        background: $accent 30%;
    }

    ThemePicker #theme-picker-hint {
        height: 1;
        color: $text-muted;
        text-align: right;
    }
    """

    class Previewed(Message):
        def __init__(self, theme_name: str) -> None:
            super().__init__()
            self.theme_name = theme_name

    class Selected(Message):
        def __init__(self, theme_name: str) -> None:
            super().__init__()
            self.theme_name = theme_name

    class Cancelled(Message):
        """The picker was dismissed without committing its preview."""

    def __init__(self, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id)
        self._title = Static("Choose a theme", id="theme-picker-title", markup=False)
        self._options = OptionList(id="theme-picker-options")
        self._hint = Static(
            "↑↓ preview · enter apply · esc cancel", id="theme-picker-hint", markup=False
        )
        self._rows: tuple[WispThemeSpec, ...] = WISP_THEME_SPECS
        self._opened_at = 0.0
        self._submitted = False

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._options
        yield self._hint

    @property
    def is_open(self) -> bool:
        return self.display

    def show(self, current_theme: str) -> None:
        self._opened_at = time.monotonic()
        self._submitted = False
        self._options.clear_options()
        current_index = 0
        for index, spec in enumerate(self._rows):
            marker = "●" if spec.name == current_theme else " "
            label = Content(f"{marker} {spec.label:<8} {spec.description}")
            self._options.add_option(Option(label, id=spec.name))
            if spec.name == current_theme:
                current_index = index
        self._options.highlighted = current_index
        self.display = True
        self._options.focus()

    def hide(self) -> None:
        self.display = False
        self._submitted = False

    def _highlighted_theme(self) -> str | None:
        index = self._options.highlighted
        if index is None or index >= len(self._rows):
            return None
        return self._rows[index].name

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list is not self._options or event.time < self._opened_at:
            return
        event.stop()
        if theme_name := self._highlighted_theme():
            self.post_message(self.Previewed(theme_name))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list is not self._options or event.time < self._opened_at:
            return
        event.stop()
        self.action_select()

    def on_key(self, event: events.Key) -> None:
        if self.is_open and event.time < self._opened_at:
            event.stop()
            event.prevent_default()

    def action_select(self) -> None:
        if self._submitted or not self.is_open:
            return
        if theme_name := self._highlighted_theme():
            self._submitted = True
            self.post_message(self.Selected(theme_name))

    def action_cancel(self) -> None:
        if self.is_open:
            self.post_message(self.Cancelled())


__all__ = ["ThemePicker"]
