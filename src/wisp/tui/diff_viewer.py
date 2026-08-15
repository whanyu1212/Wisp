"""Full-screen Textual viewer for one bounded structured tool diff."""

from __future__ import annotations

from rich.cells import cell_len
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.widgets import Static

from wisp.tui.diff_presentation import (
    DIFF_ADD_COUNT_STYLE,
    DIFF_DEL_COUNT_STYLE,
    DiffPresentation,
    DiffVisibleRow,
)
from wisp.tui.diff_rendering import render_diff_visible_row
from wisp.tui.rendering import _truncate_to_cell_width


class DiffViewer(Vertical):
    """Read one retained unified diff without changing transcript scroll state."""

    BINDING_GROUP_TITLE = "Diff viewer"
    HELP = """
    # Diff viewer

    Review the complete retained edit or write diff. Use j/k or the arrow keys to
    scroll a row at a time, PageUp/PageDown for larger moves, and Home/End for the
    beginning or end. Escape or q returns to the conversation exactly where it was.
    """
    can_focus = True
    BINDINGS = [
        Binding("escape,q", "close", "Close", show=False),
        Binding("j,down", "down", "Down", show=False),
        Binding("k,up", "up", "Up", show=False),
        Binding("pagedown,ctrl+f", "page_down", "Page down", show=False),
        Binding("pageup,ctrl+b", "page_up", "Page up", show=False),
        Binding("home", "home", "Top", show=False),
        Binding("end", "end", "Bottom", show=False),
    ]

    DEFAULT_CSS = """
    DiffViewer {
        overlay: screen;
        display: none;
        width: 100%;
        height: 100%;
        padding: 1 2;
        background: $background;
    }

    DiffViewer #diff-viewer-header {
        height: 1;
        padding: 0 1;
        color: $text;
        background: $panel;
        border-left: heavy $accent;
    }

    DiffViewer #diff-viewer-scroll {
        height: 1fr;
        margin-top: 1;
        overflow-x: hidden;
        scrollbar-size-vertical: 1;
        scrollbar-color: $accent 35%;
        scrollbar-color-hover: $accent 65%;
    }

    DiffViewer #diff-viewer-body {
        width: 100%;
        height: auto;
    }

    DiffViewer #diff-viewer-footer {
        height: 1;
        margin-top: 1;
        color: $text-muted;
        text-align: right;
    }
    """

    class Closed(Message):
        """The reader dismissed the full diff view."""

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 - Textual API
        super().__init__(id=id)
        self._presentation: DiffPresentation | None = None
        self._header = Static("", id="diff-viewer-header", markup=False)
        self._scroll = VerticalScroll(id="diff-viewer-scroll")
        self._body = Static("", id="diff-viewer-body", markup=False)
        self._footer = Static(
            "j/k scroll · PageUp/PageDown page · Home/End · q close",
            id="diff-viewer-footer",
            markup=False,
        )

    def compose(self) -> ComposeResult:
        yield self._header
        with self._scroll:
            yield self._body
        yield self._footer

    @property
    def is_open(self) -> bool:
        return self.display

    def show_diff(self, presentation: DiffPresentation) -> None:
        """Display one bounded presentation and move focus into the viewer."""

        self._presentation = presentation
        self._scroll.scroll_home(animate=False)
        self.display = True
        self._repaint()
        self.focus()
        self.call_after_refresh(self._repaint)

    def hide(self) -> None:
        self.display = False
        self._presentation = None

    def on_resize(self) -> None:
        if self.is_open:
            self._repaint()

    def action_close(self) -> None:
        if self.is_open:
            self.post_message(self.Closed())

    def action_down(self) -> None:
        self._scroll.scroll_to(y=self._scroll.scroll_y + 1, animate=False)

    def action_up(self) -> None:
        self._scroll.scroll_to(y=max(0.0, self._scroll.scroll_y - 1), animate=False)

    def action_page_down(self) -> None:
        self._scroll.scroll_page_down(animate=False)

    def action_page_up(self) -> None:
        self._scroll.scroll_page_up(animate=False)

    def action_home(self) -> None:
        self._scroll.scroll_home(animate=False)

    def action_end(self) -> None:
        self._scroll.scroll_end(animate=False)

    def _repaint(self) -> None:
        presentation = self._presentation
        if presentation is None:
            return
        width = max(12, self._scroll.content_size.width or self.size.width - 4)
        additions = f"+{presentation.additions}"
        deletions = f"-{presentation.deletions}"
        counts_width = cell_len(additions) + cell_len(deletions) + 3
        path_width = max(1, width - counts_width - 3)
        path = _truncate_to_cell_width(presentation.file_label, path_width)
        self._header.update(
            Content.styled(f"{presentation.file_marker} {path}", "b")
            + Content("  ")
            + Content.styled(additions, DIFF_ADD_COUNT_STYLE)
            + Content(" ")
            + Content.styled(deletions, DIFF_DEL_COUNT_STYLE)
        )
        content = Content("")
        for index, row in enumerate(presentation.rows):
            if index:
                content += Content("\n")
            content += render_diff_visible_row(
                DiffVisibleRow(row),
                width=width,
                show_line_numbers=presentation.show_line_numbers,
                indent="",
            )
        self._body.update(content)


__all__ = ["DiffViewer"]
