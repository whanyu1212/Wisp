"""Inline ``@``-file completion menu.

A sibling of :class:`~wisp.tui.widgets.SlashSuggest` and deliberately built to the
same contract: a passive, non-focusable projection of the input buffer, driven
entirely by the app's ``on_text_area_changed`` and ``on_key``. See the extended
note on ``can_focus`` below — it is the invariant that keeps the caret in the
editor.

Selecting a candidate inserts a *relative path only*. The file's contents are
never read here: the model reads the file through its normal tool call, so every
real read stays behind ``resolve_tool_path``/``is_protected_path`` rather than
this widget becoming a second, ungated read path.
"""

from __future__ import annotations

from textual import events
from textual.content import Content, Span
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from wisp.tui.file_index import ScoredPath, filter_paths
from wisp.tui.rendering import _truncate_to_cell_width


class FileSuggest(OptionList):
    """Inline file picker anchored above the composer, opened by typing ``@``."""

    # OptionList defaults to can_focus=True; force it off, for the same reason
    # SlashSuggest does (see its comment). This menu is a projection of the input
    # buffer driven by the app's on_key — if it could take focus, opening it on
    # `@` would steal the caret from the PromptEditor and the rest of the query
    # would land in the OptionList, which drops printable keys.
    can_focus = False

    # Mirrors SlashSuggest's geometry so both menus occupy the same anchored slot
    # above the composer. `overlay: screen` floats over the transcript without
    # reflowing it; `offset: 0 -100%` sits it immediately above the compose anchor;
    # `constrain: inside` keeps it on-screen at any terminal size. Deliberately NOT
    # on a separate `layer:` — a lone child on the overlay layer is laid out at the
    # top of the app by that layer's own vertical layout, detaching it from the prompt.
    DEFAULT_CSS = """
    FileSuggest {
        overlay: screen;
        constrain: inside;
        display: none;
        width: auto;
        max-width: 72;
        height: auto;
        max-height: 8;
        offset: 0 -100%;
        border: round $accent;
        background: $background;
        padding: 0 1;
        scrollbar-size-vertical: 1;
    }
    FileSuggest > .option-list--option-highlighted {
        background: transparent;
        color: $accent;
    }
    """

    _MAX_WIDTH_CEILING = 72

    def __init__(self, id: str | None = None) -> None:  # noqa: A002 - Textual's param name
        super().__init__(id=id)
        self._paths: tuple[str, ...] = ()
        self._visible: tuple[ScoredPath, ...] = ()
        self._max_width = self._MAX_WIDTH_CEILING

    def set_paths(self, paths: tuple[str, ...]) -> None:
        """Install the candidate corpus (collected off-thread by the app)."""

        self._paths = paths

    @property
    def has_paths(self) -> bool:
        return bool(self._paths)

    def on_resize(self, event: events.Resize) -> None:
        # Same on_resize-driven pattern as SlashSuggest and StatusBar.
        self._max_width = min(self._MAX_WIDTH_CEILING, max(1, self.screen.size.width - 4))
        self.styles.max_width = self._max_width

    @staticmethod
    def query_from_value(value: str, cursor: int) -> str | None:
        """The path fragment being typed after ``@``, or ``None`` if not in a mention.

        This deliberately inverts ``SlashSuggest.query_from_value``. A slash command
        owns the whole line, so that check anchors at position 0 and rejects any
        space. An ``@`` mention instead appears *mid-prompt* after arbitrary prose,
        so the trigger is cursor-relative: scan back from the cursor to the nearest
        ``@`` and treat what follows as the query.

        The mention must start at a word boundary (line start or whitespace), so an
        email address or a decorator (``foo@bar``, ``@property`` mid-word) doesn't
        open the menu. The query itself stops at whitespace — once the user types a
        space the mention is complete and the menu closes.
        """

        if cursor < 0 or cursor > len(value):
            return None
        head = value[:cursor]
        at_index = head.rfind("@")
        if at_index == -1:
            return None
        # A mention only begins at the start of a line or after whitespace.
        if at_index > 0 and not value[at_index - 1].isspace():
            return None
        fragment = head[at_index + 1 :]
        # Whitespace ends the mention; a newline does too (the query is one token).
        if any(character.isspace() for character in fragment):
            return None
        return fragment

    def show_for(self, value: str, cursor: int) -> int:
        """Filter and display the menu for the current buffer; return match count.

        Returns 0 and hides when the cursor isn't inside an ``@`` mention or nothing
        matches — the caller relies on the count to know whether the menu is live,
        matching SlashSuggest's contract.
        """

        query = self.query_from_value(value, cursor)
        if query is None or not self._paths:
            self.hide()
            return 0

        matches = filter_paths(self._paths, query)
        self._visible = matches
        self.clear_options()
        if not matches:
            self.display = False
            return 0

        content_width = max(1, self._max_width - 4)
        self.add_options(
            [Option(self._render_path(match, content_width), id=match.path) for match in matches]
        )
        self.highlighted = 0
        self.display = True
        return len(matches)

    def _render_path(self, match: ScoredPath, width: int) -> Content:
        """Render one candidate, underlining the characters the query matched.

        Truncation happens first, then offsets past the cut are dropped — spans
        pointing beyond the string would otherwise raise once the path is elided.
        """

        text = _truncate_to_cell_width(match.path, width)
        content = Content(text)
        spans = [
            Span(offset, offset + 1, "underline") for offset in match.offsets if offset < len(text)
        ]
        return content.add_spans(spans) if spans else content

    def hide(self) -> None:
        self.display = False
        self._visible = ()

    @property
    def is_open(self) -> bool:
        return self.display

    def highlighted_path(self) -> str | None:
        """The path under the highlight, or ``None`` when the menu is empty."""

        if self.highlighted is None:
            return None
        option = self.get_option_at_index(self.highlighted)
        return option.id
