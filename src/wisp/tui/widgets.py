"""Per-message transcript widgets for the Textual TUI.

Stage 2 replaces the append-only ``RichLog`` transcript with a
``VerticalScroll`` of these widgets, one per turn/event. Two kinds:

- ``LineMessage`` — a role-styled single block for tool calls, results,
  approvals, errors, notices, and user input. Content is escaped Rich markup in
  a ``Static`` (never fed to the Markdown parser), preserving the
  escape-at-boundary invariant for untrusted tool/error payloads.
- ``StreamMessage`` — the streaming assistant turn, backed by a ``Markdown``
  widget so model output renders code blocks, lists, and emphasis. Textual's
  native ``MarkdownStream`` incrementally appends provider fragments.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from rich.cells import cell_len
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.timer import Timer
from textual.widget import AwaitMount, Widget
from textual.widgets import (
    DataTable,
    Label,
    LoadingIndicator,
    Markdown,
    OptionList,
    RadioButton,
    RadioSet,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option

from wisp.events import (
    RpcSessionSummary,
    RpcSkillCatalogSnapshot,
    ToolApprovalRequested,
    TrustRequested,
)
from wisp.providers.catalog import ModelCatalogProviderEntry
from wisp.tui.commands import (
    MODEL_COMMAND_CLEAR_EFFORT_TOKEN,
    SLASH_COMMAND_SPECS,
    SlashCommandSpec,
    TuiCommandCatalog,
)
from wisp.tui.decision_content import (
    _approval_content,
    _bounded_tool_session_option_name,
    _DecisionContent,
    _trust_content,
)
from wisp.tui.diff_presentation import (
    DIFF_ADD_COUNT_STYLE,
    DIFF_ADD_STYLE,
    DIFF_ADD_TOKEN_STYLE,
    DIFF_CONTEXT_STYLE,
    DIFF_DEL_COUNT_STYLE,
    DIFF_DEL_STYLE,
    DIFF_DEL_TOKEN_STYLE,
    DIFF_META_STYLE,
    DiffPresentation,
    DiffRow,
    DiffRowKind,
    DiffVisibleRow,
)
from wisp.tui.overlay import TranscriptViewportState
from wisp.tui.rendering import (
    TuiViewSnapshot,
    _truncate_to_cell_width,
    format_tui_footer_text,
)
from wisp.tui.tool_call import format_tool_call_arguments

_TOOL_OUTPUT_PREVIEW_LINES = 8
_TOOL_OUTPUT_PREVIEW_BYTES = 2_000
PASTE_DISPLAY_THRESHOLD = 2_000


class PromptEditor(TextArea):
    """Multiline prompt editor with Pi-compatible submission keys."""

    BINDING_GROUP_TITLE = "Prompt editor"
    HELP = """
    # Prompt editor

    Write a prompt and press **Enter** to send it. Use **Shift+Enter** or
    **Ctrl+J** for a newline. Type `/` for commands or `@` to reference a project
    path; when a suggestion menu is visible, Enter accepts its highlighted item.
    Tool approval panels default to **1 (Approve once)**; their own contextual help
    explains every permission scope before you decide.
    """
    BINDINGS = [
        Binding("enter", "submit", "Send / accept suggestion", show=False),
        Binding("shift+enter,ctrl+j", "newline", "Newline", show=False),
    ]

    class Submitted(Message):
        """The prompt accepted by the editor.

        ``value`` is the full text sent to the model, with large-paste
        placeholders expanded to their backing content. ``display`` is the
        compact form for the transcript echo — the raw editor text with the
        ``[Pasted content #N: ...]`` markers left intact — so submitting a large
        paste doesn't mount the whole blob into the transcript. The two are equal
        when there are no large pastes.
        """

        def __init__(self, value: str, display: str) -> None:
            super().__init__()
            self.value = value
            self.display = display

    def __init__(
        self,
        *,
        placeholder: str = "",
        id: str | None = None,  # noqa: A002 - Textual's parameter name
    ) -> None:
        super().__init__(
            placeholder=placeholder,
            id=id,
            soft_wrap=True,
            show_line_numbers=False,
            highlight_cursor_line=False,
            tab_behavior="focus",
        )
        self._pending_pastes: list[tuple[str, str]] = []
        self._paste_placeholder_counter = 0

    @property
    def value(self) -> str:
        """Compatibility alias for the previous ``Input.value`` contract."""

        return self.text

    @value.setter
    def value(self, text: str) -> None:
        self.text = text
        if not text:
            self._clear_pending_paste()

    def replace_text(self, text: str, *, cursor_at_end: bool = True) -> None:
        """Replace the editing session without retaining hidden paste backing."""

        self._clear_pending_paste()
        self._paste_placeholder_counter = 0
        self.text = text
        if cursor_at_end:
            self.cursor_position = len(text)

    def restore_prompt(self, text: str) -> None:
        """Restore exact prompt text while keeping large content compact."""

        if len(text) <= PASTE_DISPLAY_THRESHOLD:
            self.replace_text(text)
            return
        self._clear_pending_paste()
        self._paste_placeholder_counter = 1
        placeholder = self._large_paste_placeholder(text, self._paste_placeholder_counter)
        self._pending_pastes.append((placeholder, text))
        self.text = placeholder
        self.cursor_position = len(placeholder)

    @property
    def cursor_position(self) -> int:
        """Return the cursor as a flat offset for ``Input`` compatibility."""

        row, column = self.cursor_location
        lines = self.text.split("\n")
        return sum(len(line) + 1 for line in lines[:row]) + column

    @cursor_position.setter
    def cursor_position(self, offset: int) -> None:
        bounded = max(0, min(offset, len(self.text)))
        before = self.text[:bounded]
        self.move_cursor((before.count("\n"), len(before.rsplit("\n", 1)[-1])))

    def on_paste(self, event: events.Paste) -> None:
        """Show a compact placeholder instead of rendering very large pasted text."""

        if len(event.text) <= PASTE_DISPLAY_THRESHOLD:
            return
        event.stop()
        event.prevent_default()
        self._show_large_paste_placeholder(event.text)

    def _show_large_paste_placeholder(self, content: str) -> None:
        """Store large pasted text and render a compact placeholder."""

        self._paste_placeholder_counter += 1
        placeholder = self._large_paste_placeholder(content, self._paste_placeholder_counter)
        self._pending_pastes.append((placeholder, content))
        if result := self._replace_via_keyboard(placeholder, *self.selection):
            self.move_cursor(result.end_location)
            self.focus()

    def _large_paste_placeholder(self, content: str, paste_number: int) -> str:
        """Build the display text for a large paste."""

        char_count = len(content)
        line_count = content.count("\n") + 1
        kb = char_count / 1024
        parts: list[str] = [f"{char_count:,} characters"]
        if line_count > 1:
            parts.append(f"{line_count} lines")
        if kb >= 1:
            parts.append(f"{kb:.1f} KB")
        return f"[Pasted content #{paste_number}: {', '.join(parts)}]"

    def _clear_pending_paste(self) -> None:
        """Forget any stored large paste content."""

        self._pending_pastes.clear()

    def text_for_submission(self) -> str:
        """Return the prompt text, expanding any intact large-paste placeholders.

        Paste records are retained for the whole edit session rather than pruned
        on intermediate editor states, so cut/paste-moving or delete/undo of a
        placeholder can't silently drop the backing content: expansion is decided
        purely by which placeholders are present in the final text. Only present
        placeholders expand (first occurrence each); absent ones are skipped. The
        record set is bounded per turn — it's cleared when the input is emptied
        after submit or an interrupt (see the ``value`` setter).
        """

        text = self.text
        for placeholder, content in self._pending_pastes:
            text = text.replace(placeholder, content, 1)
        return text

    async def on_key(self, event: events.Key) -> None:
        """Claim submission keys before TextArea's inherited editing bindings."""

        if event.key == "enter":
            self.action_submit()
            event.stop()
            event.prevent_default()
        elif event.key in {"shift+enter", "ctrl+j"}:
            self.action_newline()
            event.stop()
            event.prevent_default()

    def action_submit(self) -> None:
        """Submit the expanded value while retaining a compact transcript echo."""

        self.post_message(self.Submitted(self.text_for_submission(), self.text))

    def action_newline(self) -> None:
        self.insert("\n")


def _format_duration(seconds: float) -> str:
    """Human-terse elapsed time for a tool card: `0.3s`, `1.2s`, `12s`, `1m03s`.

    Sub-10s calls keep one decimal (a file read is often ~0.3s and the decimal is
    meaningful there); past 10s the decimal is noise, so it's dropped; past a
    minute it rolls to `Nm SSs`. Negative inputs (clock skew across the RPC
    boundary) clamp to 0.
    """

    seconds = max(seconds, 0.0)
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s"


def _has_detail(detail: str | Content | DiffPresentation) -> bool:
    """Whether a card detail carries content, for both str and Content forms.

    An empty ``str`` and an empty ``Content`` both mean "no detail", so a
    degenerate render never overwrites a real prior detail or forces an empty
    block. A ``Content`` is truthy by identity, so check its text explicitly.
    """

    if isinstance(detail, Content):
        return bool(detail.plain)
    if isinstance(detail, DiffPresentation):
        return bool(detail.rows)
    return bool(detail)


def _indent_content(detail: Content) -> Content:
    """Indent each line of a pre-styled ``Content`` by two spaces.

    Matches the plain-string detail's `"  " + line` indent, preserving the
    detail's own styled spans. The indent is trusted literal chrome; the detail's
    text stays literal (never re-parsed as markup).
    """

    lines = detail.split("\n")
    indented = Content("")
    for offset, line in enumerate(lines):
        if offset:
            indented += Content("\n")
        indented += Content("  ") + line
    return indented


def _render_diff_presentation(
    presentation: DiffPresentation,
    *,
    width: int,
    expanded: bool,
) -> Content:
    """Paint one structured diff with a fixed gutter and no ambiguous wrapping."""

    inner_width = max(1, width - 2)  # ToolCard indents details by two cells.
    additions = f"+{presentation.additions}"
    deletions = f"-{presentation.deletions}"
    counts_width = cell_len(additions) + 1 + cell_len(deletions)
    path_width = max(1, inner_width - counts_width - 4)
    path = _truncate_to_cell_width(presentation.file_label, path_width)
    header = (
        Content.styled(f"{presentation.file_marker} {path}", "b")
        + Content("  ")
        + Content.styled(additions, DIFF_ADD_COUNT_STYLE)
        + Content(" ")
        + Content.styled(deletions, DIFF_DEL_COUNT_STYLE)
    )
    content = Content("  ") + header
    for visible_row in presentation.visible_rows(expanded=expanded):
        content += Content("\n") + _render_diff_visible_row(
            visible_row,
            width=inner_width,
            show_line_numbers=presentation.show_line_numbers,
        )
    return content


def _render_diff_visible_row(
    visible_row: DiffVisibleRow,
    *,
    width: int,
    show_line_numbers: bool,
) -> Content:
    """Render one selected row, padding changed rows into a full-width band.

    Context and metadata rows are never padded — a trailing fill on an untinted
    row is invisible cells that only widen the transcript. A changed row, by
    contrast, carries a background, so its fill is exactly what turns a ragged
    coloured fragment into a band that ends at the card edge.
    """

    row = visible_row.row
    if row.kind in {DiffRowKind.omission, DiffRowKind.hunk}:
        return Content("  ") + Content.styled(
            _truncate_to_cell_width(row.text, width),
            DIFF_META_STYLE,
        )

    marker = {
        DiffRowKind.context: " ",
        DiffRowKind.addition: "+",
        DiffRowKind.deletion: "-",
    }[row.kind]
    if show_line_numbers:
        old_line = "" if row.old_line is None else str(row.old_line)
        new_line = "" if row.new_line is None else str(row.new_line)
        gutter = f"{old_line:>4} {new_line:>4} {marker} │ "
    else:
        gutter = f"{marker} │ "
    source_width = max(1, width - cell_len(gutter))
    source, emphasis_ranges = _crop_diff_row_source(row, width=source_width)
    style = _diff_row_style(row)
    content = Content.styled("  " + gutter, style) + _styled_diff_source(
        source, emphasis_ranges, _diff_token_style(row), style
    )
    if row.kind in {DiffRowKind.addition, DiffRowKind.deletion}:
        fill = source_width - cell_len(source)
        if fill > 0:
            content += Content.styled(" " * fill, style)
    return content


def _crop_diff_row_source(
    row: DiffRow,
    *,
    width: int,
) -> tuple[str, tuple[tuple[int, int], ...]]:
    """Crop literal source before its known synthetic terminator annotation."""

    note_length = min(max(0, row.terminator_note_length), len(row.text))
    source_text = row.text[:-note_length] if note_length else row.text
    note = row.text[-note_length:] if note_length else ""
    # Favor review evidence over an annotation when the gutter leaves too few
    # cells to show a useful changed token. At wider sizes, reserve the note's
    # exact known width and append it after the independently cropped literal.
    note_width = cell_len(note)
    source_width = width - note_width
    show_note = bool(note) and source_width >= 4
    cropped, ranges = _crop_diff_source(
        source_text,
        row.emphasis_ranges,
        width=source_width if show_note else width,
        preserve_tail=row.kind in {DiffRowKind.addition, DiffRowKind.deletion},
    )
    if show_note:
        return f"{cropped}{note}", ranges
    if note:
        # The annotation did not fit, so make the omitted metadata explicit
        # without allowing it to displace the source evidence. On an exact-fit
        # row, reserve the final cell for the marker rather than silently
        # making a newline-only change look identical on both sides.
        if cell_len(cropped) < width:
            return f"{cropped}…", ranges
        return f"{_take_cell_prefix(cropped, max(0, width - 1))}…", ranges
    return cropped, ranges


def _diff_row_style(row: DiffRow) -> str:
    if row.kind is DiffRowKind.addition:
        return DIFF_ADD_STYLE
    if row.kind is DiffRowKind.deletion:
        return DIFF_DEL_STYLE
    if row.kind is DiffRowKind.context:
        return DIFF_CONTEXT_STYLE
    return DIFF_META_STYLE


def _diff_token_style(row: DiffRow) -> str:
    """The stronger tint for changed tokens inside an already-tinted row.

    Only addition and deletion rows have a distinct token tint. Any other kind
    falls back to its own row style, so emphasis on an unexpected row degrades
    to no visible change rather than an unreadable inverted block.
    """

    if row.kind is DiffRowKind.addition:
        return DIFF_ADD_TOKEN_STYLE
    if row.kind is DiffRowKind.deletion:
        return DIFF_DEL_TOKEN_STYLE
    return _diff_row_style(row)


def _crop_diff_source(
    text: str,
    ranges: tuple[tuple[int, int], ...],
    *,
    width: int,
    preserve_tail: bool,
) -> tuple[str, tuple[tuple[int, int], ...]]:
    """Crop a source row while keeping its emphasized evidence in view.

    A normal width clip starts at column zero, which can hide the only changed
    token when it occurs near the end of a long line. When emphasis is present,
    reserve visible cells around its complete span and remap ranges to the cropped
    literal string. The outer gutter still identifies the row as an addition or
    deletion, while ellipses explicitly signal omitted source context.
    """

    if cell_len(text) <= width:
        return text, ranges
    if width < 3:
        # A terminal this narrow cannot accommodate both truncation markers and
        # a changed character; preserve the row's fixed +/- gutter without
        # overflowing it. Supported compact layouts have wider source columns.
        return _truncate_to_cell_width(text, width), ()
    normalized = tuple(
        sorted(
            (max(0, start), min(len(text), end))
            for start, end in ranges
            if end > start and start < len(text)
        )
    )
    if not normalized:
        # Unequal replacements intentionally skip intra-line matching. Their
        # changed rows still need reviewable evidence on narrow terminals: use
        # a literal suffix window rather than showing only a shared prefix.
        if preserve_tail and width >= 2:
            return f"…{_take_cell_suffix(text, width - 1)}", ()
        return _truncate_to_cell_width(text, width), ()

    focus_start = normalized[0][0]
    focus_end = normalized[-1][1]
    left_marker = "…" if focus_start else ""
    right_marker = "…" if focus_end < len(text) else ""
    focus_width = max(1, width - cell_len(left_marker) - cell_len(right_marker))
    focus = text[focus_start:focus_end]
    before = ""
    after = ""
    if cell_len(focus) > focus_width:
        # Prefix-clipping the emphasis itself hides changed source. Reserve a
        # trailing ellipsis even when the original span reached line end so the
        # reader knows that horizontally changed evidence remains unavailable.
        right_marker = "…"
        focus_width = max(1, width - cell_len(left_marker) - cell_len(right_marker))
        focus = _take_cell_prefix(focus, focus_width)
        focus_end = focus_start + len(focus)
    else:
        context_width = focus_width - cell_len(focus)
        before = _take_cell_suffix(text[:focus_start], context_width // 2)
        after = _take_cell_prefix(text[focus_end:], context_width - cell_len(before))

    source = f"{left_marker}{before}{focus}{after}{right_marker}"
    offset = len(left_marker) + len(before)
    remapped = tuple(
        (offset + max(start, focus_start) - focus_start, offset + min(end, focus_end) - focus_start)
        for start, end in normalized
        if max(start, focus_start) < min(end, focus_end)
    )
    return source, remapped


def _take_cell_prefix(text: str, width: int) -> str:
    """Return the longest literal prefix that fits in ``width`` terminal cells."""

    cells = 0
    end = 0
    for index, character in enumerate(text):
        character_cells = cell_len(character)
        if cells + character_cells > width:
            break
        cells += character_cells
        end = index + 1
    return text[:end]


def _take_cell_suffix(text: str, width: int) -> str:
    """Return the longest literal suffix that fits in ``width`` terminal cells."""

    cells = 0
    start = len(text)
    for index in range(len(text) - 1, -1, -1):
        character_cells = cell_len(text[index])
        if cells + character_cells > width:
            break
        cells += character_cells
        start = index
    return text[start:]


def _styled_diff_source(
    source: str,
    ranges: tuple[tuple[int, int], ...],
    token_style: str,
    base_style: str,
) -> Content:
    """Keep literal source styled while retaining bounded intra-line emphasis.

    ``token_style`` is a complete style for the changed spans rather than a
    modifier appended to ``base_style``, so emphasis is a deliberate second tint
    layered on the row band instead of an inversion of it.
    """

    if not ranges:
        return Content.styled(source, base_style) if base_style else Content(source)
    content = Content("")
    cursor = 0
    for start, end in sorted(ranges):
        start = min(max(cursor, start), len(source))
        end = min(max(start, end), len(source))
        if start > cursor:
            content += (
                Content.styled(source[cursor:start], base_style)
                if base_style
                else Content(source[cursor:start])
            )
        if end > start:
            content += Content.styled(source[start:end], token_style)
        cursor = end
    if cursor < len(source):
        content += (
            Content.styled(source[cursor:], base_style) if base_style else Content(source[cursor:])
        )
    return content


def _preview_tool_output(
    output: str,
    *,
    max_lines: int = _TOOL_OUTPUT_PREVIEW_LINES,
    max_bytes: int = _TOOL_OUTPUT_PREVIEW_BYTES,
) -> str:
    """Return bounded final tool output with explicit hidden-content metadata."""

    normalized = output.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    if not normalized.strip():
        return "(no output)"

    lines = normalized.split("\n")
    preview = "\n".join(lines[: max(1, max_lines)])
    encoded = preview.encode("utf-8")
    if len(encoded) > max_bytes:
        preview = encoded[: max(1, max_bytes)].decode("utf-8", errors="ignore")
    preview = preview.rstrip("\n")

    total_bytes = len(normalized.encode("utf-8"))
    visible_bytes = len(preview.encode("utf-8"))
    hidden_bytes = max(0, total_bytes - visible_bytes)
    if hidden_bytes == 0:
        return preview

    visible_lines = preview.count("\n") + 1
    hidden_lines = max(0, len(lines) - visible_lines)
    hidden: list[str] = []
    if hidden_lines:
        unit = "line" if hidden_lines == 1 else "lines"
        hidden.append(f"{hidden_lines} more {unit}")
    hidden.append(f"{hidden_bytes} bytes hidden")
    return f"{preview}\n... {', '.join(hidden)}"


class DecisionPanel(Vertical):
    """Approval/trust selector that temporarily replaces the composer.

    The main approval prompt defaults its highlight to "Approve once" (Enter
    approves); the YOLO-confirmation and trust prompts remain deny-first
    (Enter/Escape decline). Escape always denies/cancels on every panel,
    regardless of which option is highlighted.
    """

    BINDING_GROUP_TITLE = "Safety decision"
    HELP = """
    # Safety decision

    **Approve once** permits only this request. A tool-session choice permits the
    named tool until this Wisp process exits. **YOLO** permits every mutating and
    command tool for the process. Project trust permits loading that project's
    local configuration. Escape always chooses the conservative deny or go-back
    result.
    """
    BINDINGS = [
        Binding("1", "choose(1)", "Choose option 1", show=False),
        Binding("2", "choose(2)", "Choose option 2", show=False),
        Binding("3", "choose(3)", "Choose option 3", show=False),
        Binding("4", "choose(4)", "Choose option 4", show=False),
        Binding("escape", "conservative_cancel", "Deny / go back", show=False),
    ]

    DEFAULT_CSS = """
    DecisionPanel {
        display: none;
        width: 72;
        max-width: 90%;
        height: auto;
        max-height: 15;
        margin: 0 1;
        padding: 0 1;
        border-left: heavy $warning;
        background: $surface;
    }

    DecisionPanel #decision-title {
        height: 1;
        color: $warning;
        text-style: bold;
    }

    DecisionPanel #decision-meta {
        height: auto;
        max-height: 2;
        color: $text-muted;
    }

    DecisionPanel #decision-detail {
        height: auto;
        max-height: 5;
        padding: 0 1;
        color: $text;
    }

    DecisionPanel #decision-options {
        height: auto;
        max-height: 4;
        border: none;
        background: transparent;
        padding: 0;
        scrollbar-size: 0 0;
    }

    DecisionPanel #decision-options > .option-list--option-highlighted {
        background: $accent 30%;
    }
    """

    class Selected(Message):
        """A decision answer ready for the existing TUI prompt stream."""

        def __init__(self, answer: str) -> None:
            super().__init__()
            self.answer = answer

    def __init__(self, id: str | None = None) -> None:  # noqa: A002 - Textual's param name
        super().__init__(id=id)
        self._title = Static("", id="decision-title", markup=False)
        self._meta = Static("", id="decision-meta", markup=False)
        self._detail = Static("", id="decision-detail", markup=False)
        self._options = OptionList(id="decision-options")
        self._submitted = False
        self._mode = "approval"
        # Monotonic timestamp of the panel's most recent _show(). Widget.focus()
        # defers the actual focus change via call_later, so a key already queued
        # for the previously-focused widget (e.g. the composer) can still be
        # dispatched to this panel once focus lands — landing on whatever option
        # is highlighted by default. on_key drops any key whose event.time
        # predates this open, closing that race regardless of Textual's focus
        # scheduling. events.Key.time is stamped at event construction (when the
        # driver reads the keypress), not at dispatch, so it reliably predates
        # keys typed after the panel opened.
        self._opened_at = 0.0

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._meta
        yield self._detail
        yield self._options

    @property
    def is_open(self) -> bool:
        return self.display

    def focus_options(self) -> None:
        """Restore keyboard focus to the active decision panel's choice list."""

        if self.is_open:
            self._options.focus()

    def move_highlight_page_up(self) -> None:
        self._options.action_page_up()  # type: ignore[no-untyped-call]

    def move_highlight_page_down(self) -> None:
        self._options.action_page_down()  # type: ignore[no-untyped-call]

    def move_highlight_first(self) -> None:
        self._options.action_first()

    def move_highlight_last(self) -> None:
        self._options.action_last()

    def show_approval(self, event: ToolApprovalRequested, *, cwd: str) -> None:
        content = _approval_content(event, cwd=cwd)
        tool_name = _bounded_tool_session_option_name(event.name)
        self._show(
            content,
            options=[
                Option("1  Approve once (default)", id="approve_once"),
                Option(f"2  Allow {tool_name} for this session", id="tool_session"),
                Option("3  YOLO: allow all tools for this session", id="all_session"),
                Option("4  Deny", id="deny"),
            ],
            default_index=0,
            mode="approval",
        )

    def show_all_confirmation(self, event: ToolApprovalRequested) -> None:
        self._show(
            _DecisionContent(
                title="Enable YOLO for this TUI run?",
                meta=f"Requested while approving {event.name}",
                detail=(
                    "All mutating and command tools will run without further approval "
                    "until this Wisp process exits."
                ),
            ),
            options=[
                Option("1  Enable YOLO for this run", id="confirm_all"),
                Option("2  Go back (default)", id="cancel_all"),
            ],
            default_index=1,
            mode="all_confirmation",
        )

    def show_trust(self, event: TrustRequested) -> None:
        self._show(
            _trust_content(event),
            options=[
                Option("1  Trust project", id="approve"),
                Option("2  Keep untrusted (default)", id="deny"),
            ],
            default_index=1,
            mode="trust",
        )

    def _show(
        self,
        content: _DecisionContent,
        *,
        options: list[Option],
        default_index: int,
        mode: str,
    ) -> None:
        self._submitted = False
        self._mode = mode
        self._opened_at = time.monotonic()
        self._title.update(content.title)
        self._meta.update(content.meta)
        self._detail.update(content.detail)
        self._options.clear_options()
        self._options.add_options(options)
        self._options.highlighted = default_index
        self.display = True
        self.refresh_bindings()
        self.focus_options()

    def hide(self) -> None:
        self.display = False
        self._submitted = False

    def submit_answer(self, answer: str) -> None:
        if self._submitted or not self.is_open:
            return
        self._submitted = True
        self.post_message(self.Selected(answer))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list is not self._options:
            return
        event.stop()
        if event.time < self._opened_at:
            # A key (typically Enter) queued for the previously-focused widget
            # before this panel opened, delivered only after Widget.focus()'s
            # deferred call_later landed focus here. Drop it rather than acting
            # on whatever option happens to be highlighted. See _opened_at.
            return
        option_id = event.option.id
        if option_id is None:
            return
        answer = {
            "approve": "y",
            "approve_once": "y",
            "tool_session": "t",
            "all_session": "a",
            "confirm_all": "confirm-all",
            "cancel_all": "cancel-all",
            "deny": "n",
        }.get(option_id)
        if answer is not None:
            self.submit_answer(answer)

    def on_key(self, event: events.Key) -> None:
        if not self.is_open:
            return
        if event.time < self._opened_at:
            # A key queued before this panel opened (e.g. for the composer)
            # must not act on the newly-shown choices, INCLUDING Enter: Enter
            # is deliberately not handled below so OptionList's own native
            # enter->select binding fires normally for real presses. But that
            # binding resolution happens only if this handler doesn't stop the
            # event, so a stale Enter must be stopped explicitly here or it
            # falls through to select() and posts a freshly-timestamped
            # OptionSelected the guard in on_option_list_option_selected can no
            # longer catch (that message's .time is stamped when Textual's
            # binding machinery constructs it, i.e. "now" — never < _opened_at).
            event.stop()
            event.prevent_default()
            return

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action != "choose" or not parameters or not isinstance(parameters[0], int):
            return True
        option_count = 4 if self._mode == "approval" else 2
        return 1 <= parameters[0] <= option_count

    def action_choose(self, number: int) -> None:
        answers = {
            "approval": {1: "y", 2: "t", 3: "a", 4: "n"},
            "all_confirmation": {1: "confirm-all", 2: "cancel-all"},
            "trust": {1: "y", 2: "n"},
        }
        if answer := answers.get(self._mode, {}).get(number):
            self.submit_answer(answer)

    def action_conservative_cancel(self) -> None:
        answer = "cancel-all" if self._mode == "all_confirmation" else "n"
        self.submit_answer(answer)


class ModelPicker(Vertical):
    """Interactive `/model` selector with a model list and effort radio group.

    The provider-grouped catalog remains an ``OptionList`` because it is long and
    scrollable. The highlighted model's small mutually-exclusive effort vocabulary
    is presented as a ``RadioSet``. The model list keeps keyboard focus so up/down
    select a model, left/right stage effort, Enter applies both, and Escape cancels.
    """

    BINDING_GROUP_TITLE = "Model picker"
    HELP = """
    # Model picker

    Move through provider models with the arrow keys. For models that expose
    reasoning effort, use Left and Right to stage a level. Enter applies the
    highlighted model and Escape closes the picker without changing it.
    """
    BINDINGS = [
        Binding("left", "cycle_effort(-1)", "Lower effort", show=False),
        Binding("right", "cycle_effort(1)", "Raise effort", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    ModelPicker {
        display: none;
        height: auto;
        max-height: 20;
        margin: 0 1;
        padding: 0 1;
        border-left: heavy $accent;
        background: $surface;
    }

    ModelPicker #model-picker-title {
        height: 1;
        color: $accent;
        text-style: bold;
    }

    ModelPicker #model-picker-options {
        height: auto;
        max-height: 14;
        border: none;
        background: transparent;
        padding: 0;
    }

    ModelPicker #model-picker-options > .option-list--option-highlighted {
        background: $accent 30%;
    }

    ModelPicker #model-picker-options > .option-list--option-disabled {
        text-style: bold;
    }

    ModelPicker #model-picker-effort {
        display: none;
        height: 1;
        layout: horizontal;
        padding: 0 1;
    }

    ModelPicker #model-picker-effort-label {
        width: auto;
        height: 1;
        margin-right: 1;
        color: $text-muted;
    }

    ModelPicker #model-picker-effort > RadioSet {
        width: auto;
        height: 1;
        layout: horizontal;
        border: none;
        padding: 0;
        background: transparent;
    }

    ModelPicker #model-picker-effort > RadioSet > RadioButton {
        width: auto;
        height: 1;
        margin-right: 1;
        padding: 0;
        background: transparent;
        border: none;
    }
    """

    class Selected(Message):
        """A model (and optional effort) pick, ready for the prompt stream."""

        def __init__(self, answer: str) -> None:
            super().__init__()
            self.answer = answer

    class Cancelled(Message):
        """The picker was dismissed (Escape) without a selection."""

    def __init__(self, id: str | None = None) -> None:  # noqa: A002 - Textual's param name
        super().__init__(id=id)
        self._title = Static("Select a model", id="model-picker-title", markup=False)
        self._options = OptionList(id="model-picker-options")
        self._effort_row = Horizontal(
            Static("Effort:", id="model-picker-effort-label", markup=False),
            id="model-picker-effort",
        )
        self._effort_radio: RadioSet | None = None
        self._effort_radio_row: tuple[str, str] | None = None
        self._effort_values: dict[RadioButton, str | None] = {}
        self._effort_radio_ready = False
        self._submitted = False
        self._opened_at = 0.0
        # (provider_name, model_id) per enabled option, in the same order as
        # _options -- id-based lookup can't be used directly since an option's
        # id (a composite "provider::model" string) still needs splitting on
        # every highlight change, so this list is the single source of truth
        # for "what does the currently highlighted row mean."
        self._rows: list[tuple[str, str] | None] = []
        self._entries_by_provider: dict[str, ModelCatalogProviderEntry] = {}
        # Selected effort tier per (provider, model) touched this session --
        # preserved across highlight moves so arrowing away and back doesn't
        # forget a choice, matching how the composer never discards a draft.
        self._effort_choice: dict[tuple[str, str], str | None] = {}
        # Rows the user has explicitly cycled effort on (via left/right), even
        # if that cycling landed back on None/"(default)". Without this, "never
        # touched effort" and "explicitly cleared back to default" are both
        # `_effort_choice.get(row) is None` and indistinguishable on submit --
        # the former must omit effort from the configure call entirely (leave
        # server-side state alone), the latter must send clear_effort=True.
        self._effort_touched: set[tuple[str, str]] = set()

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._options
        yield self._effort_row

    @property
    def is_open(self) -> bool:
        return self.display

    def focus_options(self) -> None:
        """Restore keyboard focus to the picker's model list."""

        if self.is_open:
            self._options.focus()

    def show(
        self,
        entries: tuple[ModelCatalogProviderEntry, ...],
        *,
        current_provider: str,
        current_model: str | None,
        current_effort: str | None,
    ) -> None:
        self._submitted = False
        self._opened_at = time.monotonic()
        self._entries_by_provider = {entry.name: entry for entry in entries}
        self._effort_choice = {}
        self._effort_touched = set()
        self._options.clear_options()
        self._rows = []
        default_index: int | None = None
        first_selectable_index: int | None = None
        row_index = 0
        for entry in entries:
            self._options.add_option(Option(entry.name, disabled=True))
            self._rows.append(None)
            row_index += 1
            is_current_provider = entry.name == current_provider
            effective_model = (
                entry.canonical_model(current_model)
                if current_model is not None
                else entry.default_model
            )
            for model_id in entry.models:
                if first_selectable_index is None:
                    first_selectable_index = row_index
                is_current = is_current_provider and model_id == effective_model
                if is_current:
                    default_index = row_index
                    # Defense in depth: current_effort is a caller-supplied
                    # value, not guaranteed valid for this exact model's
                    # catalog-listed tiers (effort is provider-native,
                    # non-normalized -- see ModelCatalogProviderEntry). Seeding
                    # a tier this row doesn't list would let an untouched
                    # Enter resubmit it verbatim (see submit_current_selection).
                    seeded_effort = current_effort
                    if seeded_effort is not None and seeded_effort not in entry.effort_levels.get(
                        model_id, ()
                    ):
                        seeded_effort = None
                    self._effort_choice[(entry.name, model_id)] = seeded_effort
                lifecycle = entry.model_lifecycle.get(model_id)
                lifecycle_label = f" ({lifecycle})" if lifecycle not in (None, "stable") else ""
                current_label = " (current)" if is_current else ""
                label = f"  {model_id}{lifecycle_label}{current_label}"
                self._options.add_option(Option(label, id=f"{entry.name}::{model_id}"))
                self._rows.append((entry.name, model_id))
                row_index += 1
        # A current_provider/current_model not present in the catalog (e.g.
        # after a permissive /model <unknown-model>) leaves no row matching
        # `is_current` -- fall back to the first selectable model row rather
        # than defaulting to index 0, the first (disabled) provider header,
        # which would leave the picker opened on a non-interactive row where
        # Enter does nothing until the user manually navigates.
        self._options.highlighted = (
            default_index if default_index is not None else first_selectable_index
        )
        self._update_effort_control()
        self.display = True
        self.focus_options()

    def hide(self) -> None:
        self.display = False
        self._submitted = False

    def _highlighted_row(self) -> tuple[str, str] | None:
        highlighted = self._options.highlighted
        if highlighted is None or highlighted >= len(self._rows):
            return None
        return self._rows[highlighted]

    def _effort_levels_for(self, row: tuple[str, str]) -> tuple[str, ...]:
        provider_name, model_id = row
        entry = self._entries_by_provider.get(provider_name)
        if entry is None:
            return ()
        return entry.effort_levels.get(model_id, ())

    def _update_effort_control(self) -> None:
        """Show a fresh radio group for the highlighted model's effort tiers."""

        row = self._highlighted_row()
        levels = self._effort_levels_for(row) if row is not None else ()
        if self._effort_radio is not None:
            self._effort_radio.remove()
        self._effort_radio = None
        self._effort_radio_row = None
        self._effort_values = {}
        self._effort_radio_ready = False
        if row is None or not levels:
            self._effort_row.display = False
            return

        chosen = self._effort_choice.get(row)
        choices: tuple[str | None, ...] = (None, *levels)
        buttons = tuple(
            RadioButton("Default" if value is None else value, value=value == chosen)
            for value in choices
        )
        radio = RadioSet(*buttons, name="reasoning-effort", compact=True)
        # The model OptionList remains the keyboard owner. Radio buttons are still
        # clickable, while ModelPicker routes left/right itself.
        radio.can_focus = False
        for button in buttons:
            button.can_focus = False
        self._effort_radio = radio
        self._effort_radio_row = row
        self._effort_values = dict(zip(buttons, choices, strict=True))
        self._effort_row.display = True
        self._effort_row.mount(radio)
        self.call_after_refresh(self._mark_effort_radio_ready, radio)

    def _mark_effort_radio_ready(self, radio: RadioSet) -> None:
        if self._effort_radio is radio:
            self._effort_radio_ready = True

    def _sync_effort_radio(self) -> None:
        row = self._highlighted_row()
        if row is None or row != self._effort_radio_row:
            return
        chosen = self._effort_choice.get(row)
        for button, value in self._effort_values.items():
            if value == chosen:
                button.value = True
                return

    def cycle_effort(self, *, direction: int) -> None:
        """Move the highlighted model's effort choice by one step.

        ``direction`` is ``1`` (right/higher) or ``-1`` (left/lower). Moving
        left from the lowest tier (or from any tier when the model has none
        chosen yet is already the leftmost state) lands on "(default)" --
        moving further left than that is a no-op, not a wrap-around, since
        "default" isn't a real tier to cycle past.
        """

        row = self._highlighted_row()
        if row is None:
            return
        levels = self._effort_levels_for(row)
        if not levels:
            return
        current = self._effort_choice.get(row)
        if current is None:
            index = -1
        else:
            try:
                index = levels.index(current)
            except ValueError:
                index = -1
        new_index = index + direction
        if new_index < -1:
            new_index = -1
        elif new_index >= len(levels):
            new_index = len(levels) - 1
        self._effort_choice[row] = None if new_index == -1 else levels[new_index]
        self._effort_touched.add(row)
        self._sync_effort_radio()

    def submit_current_selection(self) -> None:
        if self._submitted or not self.is_open:
            return
        row = self._highlighted_row()
        if row is None:
            return
        provider_name, model_id = row
        # provider::model, not a bare model id -- a model id is not unique
        # across providers (e.g. "gpt-5.5" is claimed by both openai and
        # openai-codex), and TuiShell._handle_model_command would otherwise
        # have to guess a provider for a shared id via ModelRegistry.resolve's
        # ambiguity handling, silently leaving an explicitly-picked row's
        # provider unapplied. See MODEL_COMMAND_CLEAR_EFFORT_TOKEN for the
        # effort token's three-state encoding (omitted / explicit tier /
        # explicit clear).
        target = f"{provider_name}::{model_id}"
        effort = self._effort_choice.get(row)
        if effort is not None:
            answer = f"/model {target} {effort}"
        elif row in self._effort_touched:
            answer = f"/model {target} {MODEL_COMMAND_CLEAR_EFFORT_TOKEN}"
        else:
            answer = f"/model {target}"
        self._submitted = True
        self.post_message(self.Selected(answer))

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list is not self._options:
            return
        event.stop()
        self._update_effort_control()
        self.refresh_bindings()

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.radio_set is not self._effort_radio:
            return
        event.stop()
        row = self._highlighted_row()
        if (
            not self._effort_radio_ready
            or row is None
            or row != self._effort_radio_row
            or event.pressed not in self._effort_values
        ):
            return
        self._effort_choice[row] = self._effort_values[event.pressed]
        self._effort_touched.add(row)
        self.focus_options()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list is not self._options:
            return
        event.stop()
        if event.time < self._opened_at:
            # Same stale-event guard DecisionPanel uses: a key queued for the
            # previously-focused widget before this panel opened, delivered
            # only after Widget.focus()'s deferred call_later landed focus
            # here. See DecisionPanel's identical comment for the full
            # explanation of why this can't be caught any other way.
            return
        self.submit_current_selection()

    def on_key(self, event: events.Key) -> None:
        if not self.is_open:
            return
        if event.time < self._opened_at:
            event.stop()
            event.prevent_default()
            return

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "cycle_effort":
            row = self._highlighted_row()
            return row is not None and bool(self._effort_levels_for(row))
        return True

    def action_cycle_effort(self, direction: int) -> None:
        self.cycle_effort(direction=direction)

    def action_cancel(self) -> None:
        self.post_message(self.Cancelled())


_SESSION_TABLE_MIN_WIDTH = 96


@dataclass(frozen=True)
class _SessionPickerRow:
    """One stable persisted-session identity shared by both picker layouts."""

    session_id: str
    name: str
    updated: str
    entry_count: int
    path: str
    current: bool

    @property
    def marker(self) -> str:
        return "●" if self.current else " "


class SessionPicker(Vertical):
    """Interactive newest-first RPC session selector used by bare ``/resume``."""

    BINDING_GROUP_TITLE = "Session picker"
    HELP = """
    # Session picker

    Browse persisted sessions with the navigation keys. Enter resumes the
    highlighted session; Escape closes the picker without changing sessions.
    Wide terminals use a table and narrow terminals use the same data as a list.
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    DEFAULT_CSS = """
    SessionPicker {
        display: none;
        height: auto;
        max-height: 20;
        margin: 0 1;
        padding: 0 1;
        border-left: heavy $accent;
        background: $surface;
    }

    SessionPicker #session-picker-title {
        height: 1;
        color: $accent;
        text-style: bold;
    }

    SessionPicker #session-picker-options,
    SessionPicker #session-picker-table {
        height: auto;
        max-height: 15;
        border: none;
        background: transparent;
        padding: 0;
    }

    SessionPicker #session-picker-table {
        display: none;
    }

    SessionPicker #session-picker-options > .option-list--option-highlighted,
    SessionPicker #session-picker-table > .datatable--cursor {
        background: $accent 30%;
    }

    SessionPicker #session-picker-table > .datatable--header {
        background: $panel;
        color: $text-muted;
    }

    SessionPicker #session-picker-hint {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }
    """

    class Selected(Message):
        """One persisted session chosen for the shell's typed command path."""

        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    class Cancelled(Message):
        """The picker was dismissed without changing session."""

    def __init__(self, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id)
        self._title = Static("Resume a session", id="session-picker-title", markup=False)
        self._options = OptionList(id="session-picker-options")
        self._table: DataTable[Content] = DataTable(
            cursor_type="row",
            show_row_labels=False,
            zebra_stripes=True,
            id="session-picker-table",
        )
        self._hint = Static(
            "enter select · esc cancel",
            id="session-picker-hint",
            markup=False,
        )
        self._rows: tuple[_SessionPickerRow, ...] = ()
        self._row_indices: dict[str, int] = {}
        self._table_layout = False
        self._submitted = False
        self._opened_at = 0.0

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._options
        yield self._table
        yield self._hint

    @property
    def is_open(self) -> bool:
        return self.display

    def focus_options(self) -> None:
        if self.is_open:
            (self._table if self._table_layout else self._options).focus()

    def move_highlight_page_up(self) -> None:
        if self._table_layout:
            self._table.action_page_up()
        else:
            self._options.action_page_up()  # type: ignore[no-untyped-call]

    def move_highlight_page_down(self) -> None:
        if self._table_layout:
            self._table.action_page_down()
        else:
            self._options.action_page_down()  # type: ignore[no-untyped-call]

    def move_highlight_first(self) -> None:
        self._move_highlight_to(0)

    def move_highlight_last(self) -> None:
        self._move_highlight_to(len(self._rows) - 1)

    def show(
        self,
        sessions: tuple[RpcSessionSummary, ...],
        *,
        selected_session_id: str | None,
    ) -> None:
        self._submitted = False
        self._opened_at = time.monotonic()
        self._rows = tuple(
            _SessionPickerRow(
                session_id=session.session_id,
                name=session.name or session.session_id[:12],
                updated=session.updated_at.isoformat(timespec="minutes"),
                entry_count=session.entry_count,
                path=str(session.session_path),
                current=session.session_id == selected_session_id,
            )
            for session in sessions
        )
        self._row_indices = {row.session_id: index for index, row in enumerate(self._rows)}
        self._populate_options()
        self._populate_table()
        if self._rows:
            initial_session_id = (
                selected_session_id
                if selected_session_id in self._row_indices
                else self._rows[0].session_id
            )
            self._hint.update("enter select · esc cancel")
        else:
            initial_session_id = None
            self._hint.update("esc close")
        self.display = True
        self._sync_layout(focus=False)
        if initial_session_id is not None:
            self._select_session(initial_session_id)
        self.focus_options()

    def hide(self) -> None:
        self.display = False
        self._submitted = False

    def submit_current_selection(self) -> None:
        self._submit_session(self._highlighted_session_id())

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list is not self._options:
            return
        event.stop()
        if event.time < self._opened_at:
            return
        self._submit_session(event.option.id)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table is not self._table:
            return
        event.stop()
        if event.time < self._opened_at:
            return
        self._submit_session(event.row_key.value)

    def on_resize(self, event: events.Resize) -> None:
        if self.is_open:
            self._sync_layout(focus=True)

    def on_key(self, event: events.Key) -> None:
        if not self.is_open:
            return
        if event.time < self._opened_at:
            event.prevent_default()
            event.stop()
            return
        if event.key == "pageup":
            self.move_highlight_page_up()
            event.prevent_default()
            event.stop()
        elif event.key == "pagedown":
            self.move_highlight_page_down()
            event.prevent_default()
            event.stop()
        elif event.key == "home":
            self.move_highlight_first()
            event.prevent_default()
            event.stop()
        elif event.key == "end":
            self.move_highlight_last()
            event.prevent_default()
            event.stop()

    def action_cancel(self) -> None:
        self.post_message(self.Cancelled())

    def _populate_options(self) -> None:
        self._options.clear_options()
        for row in self._rows:
            name = _truncate_to_cell_width(row.name, 48)
            path = _truncate_to_cell_width(row.path, 72)
            label = f"{row.marker} {name} · {row.entry_count} entries · {row.updated}\n  {path}"
            # Persisted names and paths are untrusted display text. Content()
            # keeps bracket syntax literal instead of letting Option parse it as
            # Textual markup.
            self._options.add_option(Option(Content(label), id=row.session_id))
        if not self._rows:
            self._options.add_option(Option("No persisted sessions found.", disabled=True))

    def _populate_table(self) -> None:
        self._table.clear(columns=True)
        self._table.add_column("", width=1, key="current")
        self._table.add_column("Session", width=24, key="session")
        self._table.add_column("Updated", width=22, key="updated")
        self._table.add_column("Entries", width=7, key="entries")
        self._table.add_column("Path", width=27, key="path")
        for row in self._rows:
            self._table.add_row(
                Content(row.marker),
                Content(_truncate_to_cell_width(row.name, 24)),
                Content(row.updated),
                Content(str(row.entry_count)),
                Content(_truncate_to_cell_width(row.path, 27)),
                key=row.session_id,
            )
        if not self._rows:
            self._table.add_row(
                Content(""),
                Content("No persisted sessions found."),
                Content(""),
                Content(""),
                Content(""),
                key=None,
            )

    def _sync_layout(self, *, focus: bool) -> None:
        table_layout = self.screen.size.width >= _SESSION_TABLE_MIN_WIDTH
        if table_layout == self._table_layout:
            return
        selected_session_id = self._highlighted_session_id()
        self._table_layout = table_layout
        self._options.display = not table_layout
        self._table.display = table_layout
        if selected_session_id is not None:
            self._select_session(selected_session_id)
        elif self._rows:
            self._move_highlight_to(0)
        if focus:
            self.focus_options()

    def _highlighted_session_id(self) -> str | None:
        if self._table_layout:
            row_index = self._table.cursor_row
            return self._rows[row_index].session_id if row_index < len(self._rows) else None
        highlighted = self._options.highlighted
        return (
            self._rows[highlighted].session_id
            if highlighted is not None and highlighted < len(self._rows)
            else None
        )

    def _select_session(self, session_id: str) -> None:
        row_index = self._row_indices.get(session_id)
        if row_index is None:
            return
        self._options.highlighted = row_index
        self._table.move_cursor(row=row_index, column=0, animate=False, scroll=False)

    def _move_highlight_to(self, row_index: int) -> None:
        if not self._rows:
            return
        bounded_index = min(max(0, row_index), len(self._rows) - 1)
        if self._table_layout:
            self._table.move_cursor(row=bounded_index, column=0, animate=False)
        else:
            self._options.highlighted = bounded_index

    def _submit_session(self, session_id: str | None) -> None:
        if self._submitted or not self.is_open or session_id not in self._row_indices:
            return
        self._submitted = True
        self.post_message(self.Selected(session_id))


class TranscriptEmptyState(Vertical):
    """Centered welcome panel shown only while the transcript has no output.

    A native ``Label`` inside a fixed-width ``Center`` provides a compact
    badge above the tagline, prompt hint, and quick-action reminder without
    consuming a permanent header row. Every direct child has the same explicit width because
    Textual centers these siblings as a block rather than independently.
    """

    DEFAULT_CSS = """
    TranscriptEmptyState.-compact #transcript-empty-actions {
        display: none;
    }

    TranscriptEmptyState.-minimal #transcript-empty-tagline,
    TranscriptEmptyState.-minimal #transcript-empty-hint {
        display: none;
    }
    """

    def __init__(self, wordmark: str, tagline: str, hint: str) -> None:
        super().__init__(id="transcript-empty")
        self._wordmark = wordmark
        self._tagline = tagline
        self._hint = hint

    @staticmethod
    def _centered(widget: Widget) -> Widget:
        widget.styles.width = 40
        return widget

    def compose(self) -> ComposeResult:
        yield self._centered(
            Center(
                Label(self._wordmark, id="transcript-empty-wordmark", markup=False),
                id="transcript-empty-wordmark-frame",
            )
        )
        yield self._centered(Label(self._tagline, id="transcript-empty-tagline", markup=False))
        yield self._centered(Label(self._hint, id="transcript-empty-hint", markup=False))
        yield self._centered(
            Static(
                "/ commands  ·  /resume session",
                id="transcript-empty-actions",
                markup=False,
            )
        )

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(self.size.height < 9, "-compact")
        self.set_class(self.size.height < 6, "-minimal")


class OperationIndicator(Vertical):
    """Centered native loading surface for an active session operation.

    The app maps typed operation state to literal labels; this widget only
    presents the current label and never decides when an operation begins or
    ends. Its full-screen overlay does not participate in transcript layout.
    """

    DEFAULT_CSS = """
    OperationIndicator {
        overlay: screen;
        display: none;
        width: 100%;
        height: 100%;
        align: center middle;
        background: transparent;
    }

    OperationIndicator #operation-indicator-panel {
        width: auto;
        height: 3;
        padding: 0 2;
        border: heavy $accent;
        background: $panel;
        align-vertical: middle;
    }

    OperationIndicator #operation-indicator-spinner {
        width: 9;
        height: 1;
        min-height: 1;
        color: $accent;
    }

    OperationIndicator #operation-indicator-label {
        width: auto;
        height: 1;
        color: $text;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 - Textual API
        super().__init__(id=id)
        self._label = Label("", id="operation-indicator-label", markup=False)

    def compose(self) -> ComposeResult:
        with Horizontal(id="operation-indicator-panel"):
            yield LoadingIndicator(id="operation-indicator-spinner")
            yield self._label

    @property
    def is_open(self) -> bool:
        """Whether the operation surface is currently visible."""

        return self.display

    def show_operation(self, label: str) -> None:
        """Display a caller-owned operation label beside the native spinner."""

        self._label.update(label)
        self.display = True

    def hide(self) -> None:
        """Hide the operation surface through the generic overlay protocol."""

        self.display = False


class JumpToLatest(Static):
    """Clickable overlay that reports logical output unseen below the viewport."""

    DEFAULT_CSS = """
    JumpToLatest {
        display: none;
        width: auto;
        max-width: 16;
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $primary;
    }

    JumpToLatest:hover {
        background: $surface-lighten-1;
        color: $text;
    }
    """

    class Selected(Message):
        """The user requested a return to the newest transcript output."""

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 - Textual API
        super().__init__("", id=id, markup=False)

    def show_count(self, count: int) -> None:
        bounded = "99+" if count > 99 else str(max(1, count))
        self.update(f"↓ {bounded} new")
        if self.parent is not None:
            self.parent.display = True
        self.display = True

    def hide(self) -> None:
        self.display = False
        if self.parent is not None:
            self.parent.display = False

    def on_click(self, event: events.Click) -> None:
        if event.button != 1:
            return
        event.stop()
        self.post_message(self.Selected())


class Transcript(VerticalScroll):
    """Scrollable message container that follows the newest output like `tail -f`.

    Auto-scroll is driven by a sticky ``_follow`` flag rather than a per-append
    "am I near the bottom?" measurement. That measurement is self-defeating while
    streaming: the growing content is what pushes the bottom away, so a snapshot
    taken as it grows reads "not at the bottom" and abandons following the very
    output it should track.

    Instead the flag tracks whether the viewport is resting at the bottom, updated
    only when the scroll position *settles* (``watch_scroll_y``):

    - Rest at the bottom → ``True`` (keep following new output).
    - The user scrolls up and away → ``False`` (they're reading history; don't
      yank them back). Scrolling back to the bottom flips it ``True`` again.

    Content growth alone never flips the flag: appends don't move ``scroll_y``,
    and ``follow_tail()``'s programmatic scroll lands *at* the end, which
    re-derives to ``True`` — self-consistent, so no guard is needed. After each
    append the app calls ``follow_tail()``, which scrolls to the end iff the flag
    is set.
    """

    BINDING_GROUP_TITLE = "Conversation"
    HELP = """
    # Conversation

    Page through the transcript with PageUp and PageDown. Home loads the oldest
    available history and End returns to live output. Wisp follows new output only
    while the viewport is resting at the latest message.
    """

    class FollowChanged(Message):
        """The viewport entered or left sticky tail-follow mode."""

        def __init__(self, following: bool) -> None:
            super().__init__()
            self.following = following

    class NeedMoreHistory(Message):
        """The user reached the oldest mounted transcript content."""

    def __init__(
        self,
        *args: object,
        empty_wordmark: str | None = None,
        empty_tagline: str = "",
        empty_hint: str = "",
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        # Textual defaults to two rows per wheel event. Keep ordinary transcript
        # scrolling and the forwarded jump-overlay path on the same one-row step.
        self.scroll_sensitivity_y = 1.0
        self._follow = True
        self._follow_generation = 0
        self._empty_wordmark = empty_wordmark
        self._empty_tagline = empty_tagline
        self._empty_hint = empty_hint
        self._empty_state: TranscriptEmptyState | None = None
        self._has_more_history = False
        self._has_retained_history = False
        self._history_loading = False
        self._history_request_armed = True

    def compose(self) -> ComposeResult:
        if self._empty_wordmark is not None:
            self._empty_state = TranscriptEmptyState(
                self._empty_wordmark,
                self._empty_tagline,
                self._empty_hint,
            )
            yield self._empty_state

    def mount_message(self, widget: Widget, *, before: Widget | None = None) -> AwaitMount:
        """Mount output after permanently dismissing the initial empty state."""

        empty_state = self._empty_state
        if empty_state is not None:
            self._empty_state = None
            empty_state.display = False
            empty_state.remove()
        return self.mount(widget, before=before)

    def clear_messages(self) -> None:
        """Remove every mounted transcript item and restore tail-follow state."""

        self._empty_state = None
        self._follow = True
        self._has_more_history = False
        self._has_retained_history = False
        self._history_loading = False
        self._history_request_armed = True
        self.remove_children()
        self.scroll_home(animate=False)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        # Textual updates scroll_y as the position settles (including at the end
        # of an animated user scroll). Re-derive follow intent from the resting
        # position: at the bottom means "keep following", anywhere above means
        # "the user is reading back, leave them there".
        previous = self._follow
        super().watch_scroll_y(old_value, new_value)
        self._follow = self.is_vertical_scroll_end
        if self._follow != previous:
            if not self._follow:
                self._follow_generation += 1
            self.post_message(self.FollowChanged(self._follow))
        if new_value > 0:
            self._history_request_armed = True
        else:
            self._request_more_history_if_needed()

    def history_page_loaded(self, *, has_more: bool) -> None:
        """Record one completed history page and its continuation state."""

        self._has_more_history = has_more
        self._history_loading = False
        self._history_request_armed = has_more or self._has_retained_history

    def history_window_available(self, *, has_older: bool) -> None:
        """Record whether the UI can shift to already retained history."""

        self._has_retained_history = has_older
        if has_older:
            self._history_request_armed = True

    @property
    def has_more_history(self) -> bool:
        """Whether a durable history page remains available."""

        return self._has_more_history

    def request_history_at_top(self) -> None:
        """Request another page only when the settled viewport remains at the top."""

        if self.scroll_y == 0:
            self._request_more_history_if_needed()

    def _request_more_history_if_needed(self) -> None:
        if not (
            (self._has_more_history or self._has_retained_history)
            and not self._history_loading
            and self._history_request_armed
        ):
            return
        self._history_loading = True
        self._history_request_armed = False
        self.post_message(self.NeedMoreHistory())

    def history_page_request_failed(self) -> None:
        """Allow a transient page-load failure to be retried at the top."""

        self._history_loading = False
        self._history_request_armed = True

    @property
    def is_following(self) -> bool:
        """Whether new output should remain pinned to the transcript tail."""

        return self._follow

    @property
    def follow_generation(self) -> int:
        """Return the latest explicit reader navigation generation."""

        return self._follow_generation

    def viewport_state(self) -> TranscriptViewportState:
        """Capture the current viewport offset and tail-follow intent."""

        return TranscriptViewportState(scroll_y=self.scroll_y, following=self._follow)

    def restore_viewport_state(self, state: TranscriptViewportState) -> None:
        """Restore a viewport snapshot after temporary layout changes."""

        if state.following:
            self.return_to_latest()
            return
        previous = self._follow
        target_y = min(max(0.0, state.scroll_y), self.max_scroll_y)
        self.scroll_to(y=target_y, animate=False)
        self._follow = False
        if previous:
            self.post_message(self.FollowChanged(False))

    def restore_prepend_viewport(
        self,
        *,
        scroll_y: float,
        anchor: Widget | None,
        anchor_y_before: float,
        following: bool,
    ) -> None:
        """Keep the same content visible after older entries were prepended."""

        if following:
            self.return_to_latest()
            return
        height_delta = max(0.0, anchor.region.y - anchor_y_before) if anchor is not None else 0.0
        self.restore_viewport_state(
            TranscriptViewportState(
                scroll_y=scroll_y + height_delta,
                following=False,
            )
        )

    def follow_tail(self) -> None:
        """Scroll to the newest content iff the user hasn't scrolled away."""
        if self._follow:
            self.scroll_end(animate=False)

    def page_up(self) -> None:
        """Move away from the tail before a page-up layout can re-pin it."""

        self._stop_following()
        self.scroll_page_up(animate=False)

    def page_down(self) -> None:
        """Scroll one transcript page without Textual's default animation."""

        self.scroll_page_down(animate=False)

    def scroll_to_oldest(self) -> None:
        """Move to the durable-history boundary without waiting for scroll settlement."""

        self._stop_following()
        self.scroll_home(animate=False)

    def stop_following(self) -> None:
        """Record explicit reader intent before a wheel scroll is processed."""

        self._stop_following()

    def _stop_following(self) -> None:
        self._follow_generation += 1
        if not self._follow:
            return
        self._follow = False
        self.post_message(self.FollowChanged(False))

    def return_to_latest(self) -> None:
        """Restore tail-follow intent and jump to the newest output immediately."""

        was_following = self._follow
        self._follow_generation += 1
        self._follow = True
        self.scroll_end(animate=False)
        if not was_following:
            self.post_message(self.FollowChanged(True))


class SlashSuggest(OptionList):
    """Inline slash-command completion menu, Claude-Code style.

    A non-modal dropdown anchored above the input: when the line starts with `/`,
    it lists the matching commands and filters live as the user types. The input
    is never touched — this widget is a hint + completion shortcut layer. It floats
    on the overlay layer so it doesn't reflow the transcript.

    The command table is `SLASH_COMMAND_SPECS` (shared with the parser), so the
    menu, Tab-completion, and `/command` execution all derive from one source. Each
    option's id is the command spelling (`/model`), so the highlighted spec is
    recovered by id — no parallel index to keep in sync.
    """

    # OptionList defaults to can_focus=True; force it off. This menu is a passive
    # projection of the input buffer (Claude-Code/Codex/Pi model) driven entirely
    # by the app's on_key (cursor moves, Tab-complete, hide) — it must NEVER become
    # a keyboard target. If it could take focus, opening it on `/` would steal the
    # caret from the PromptEditor, so the rest of the command ("quit") would land in
    # the OptionList (which drops printable keys) and only `/` would ever submit.
    can_focus = False

    # overlay: screen floats the menu over the transcript WITHOUT reflowing it.
    # `offset: 0 -100%` places it immediately above its compose anchor, so it
    # cannot cover the editor. It is deliberately NOT put on a separate `layer:`
    # — a lone child on the overlay layer gets laid out at the TOP of the app by
    # that layer's own vertical layout, detaching it from the prompt. `constrain:
    # inside` keeps the offset menu fully on-screen at any terminal size.
    DEFAULT_CSS = """
    SlashSuggest {
        overlay: screen;
        constrain: inside;
        display: none;
        width: auto;
        max-width: 64;
        height: auto;
        max-height: 8;
        offset: 0 -100%;
        border: round $accent;
        background: $background;
        padding: 0 1;
        scrollbar-size-vertical: 1;
    }
    SlashSuggest > .option-list--option-highlighted {
        background: transparent;
        color: $accent;
    }
    """

    # CSS max-width ceiling on wide terminals (DEFAULT_CSS above); on_resize
    # narrows self.styles.max_width below this only when the screen itself
    # can't fit it. Tracked here too (not read back from styles.max_width,
    # a Scalar) so show_for's column-alignment truncation has a plain int
    # content-width budget to compute against.
    _MAX_WIDTH_CEILING = 64

    def __init__(self, id: str | None = None) -> None:  # noqa: A002 - Textual's param name
        super().__init__(id=id)
        self._specs = SLASH_COMMAND_SPECS
        self._skill_specs: tuple[SlashCommandSpec, ...] = ()
        # spelling → spec, so the highlighted option's id maps back to its command.
        self._by_command: dict[str, SlashCommandSpec] = {spec.command: spec for spec in self._specs}
        self._visible_specs: tuple[SlashCommandSpec, ...] = ()
        self._max_width = self._MAX_WIDTH_CEILING

    def set_catalog(self, catalog: TuiCommandCatalog) -> None:
        self._specs = catalog.specs
        self._rebuild_command_index()
        self.hide()

    def set_skill_catalog(self, catalog: RpcSkillCatalogSnapshot) -> None:
        """Apply deterministic skill rows used only after the `/skill:` prefix."""

        self._skill_specs = tuple(
            SlashCommandSpec(
                command=f"/skill:{entry.name}",
                description=" ".join(entry.description.split()),
                takes_args=True,
            )
            for entry in catalog.entries
        )
        self._rebuild_command_index()
        self.hide()

    def _rebuild_command_index(self) -> None:
        self._by_command = {spec.command: spec for spec in (*self._specs, *self._skill_specs)}

    def on_resize(self, event: events.Resize) -> None:
        # Same `on_resize`-driven pattern as StatusBar (widgets.py, below).
        self._max_width = min(self._MAX_WIDTH_CEILING, max(1, self.screen.size.width - 4))
        self.styles.max_width = self._max_width

    @staticmethod
    def query_from_value(value: str) -> str | None:
        """The command token to filter on, or None if the value isn't a bare `/…`.

        A menu is warranted only while the *first* token is a slash word still
        being typed: the value starts with `/` and has no space yet (a space means
        the user has moved on to arguments or prose). Returns the lowercased token
        including the leading slash, e.g. `/mo`.
        """

        if not value.startswith("/") or " " in value:
            return None
        return value.lower()

    def matches(self, query: str) -> tuple[SlashCommandSpec, ...]:
        """Specs whose command starts with `query` (prefix match on the spelling)."""

        specs = self._skill_specs if query.startswith("/skill:") else self._specs
        return tuple(spec for spec in specs if spec.command.startswith(query))

    def show_for(self, value: str) -> int:
        """Filter and display the menu for the current input value.

        Returns the number of matches. Hides the menu (returns 0) when the value
        isn't a bare slash token or nothing matches — the caller relies on the
        count to know whether the menu is live.
        """

        query = self.query_from_value(value)
        specs = self.matches(query) if query is not None else ()
        self._visible_specs = specs
        self.clear_options()
        if not specs:
            self.display = False
            return 0
        # Pad every command to the widest *currently visible* spelling so
        # descriptions line up in a column, tightening as the user filters
        # rather than reserving space for commands no longer shown. At
        # narrow widths, truncate the description to fit the menu's content
        # width (max_width minus the 2-cell border + 2-cell padding).
        name_width = max(len(spec.command) for spec in specs)
        content_width = max(1, self._max_width - 4)
        self.add_options(
            [
                Option(
                    Content(
                        _truncate_to_cell_width(
                            f"{spec.command:<{name_width}}  {spec.description}", content_width
                        )
                    ),
                    id=spec.command,
                )
                for spec in specs
            ]
        )
        self.highlighted = 0
        self.display = True
        return len(specs)

    def hide(self) -> None:
        self.display = False
        self._visible_specs = ()

    @property
    def is_open(self) -> bool:
        return self.display

    def highlighted_spec(self) -> SlashCommandSpec | None:
        """The spec under the highlight, for Tab-completion; None if menu empty."""

        if self.highlighted is None:
            return None
        option = self.get_option_at_index(self.highlighted)
        return self._by_command.get(option.id or "")


# CSS role classes style every message independently of visible labels.
# Conversation roles intentionally have no border title; their role metadata and
# colored left rail remain. ToolCard.set_state() uses the remaining labels, with
# its own _STATUS_LABELS override for statuses such as cancelled.
_ROLE_LABELS: dict[str, str] = {
    "user": "",
    "assistant": "",
    "tool": "tool",
    "approved": "tool",
    "denied": "denied",
    "error": "error",
    "notice": "wisp",
    "dim": "",
    "session": "",
}


class LineMessage(Static):
    """A single role-styled transcript line for non-streamed content."""

    def __init__(self, markup: str, *, role: str) -> None:
        # `markup` is escaped message content composed by the caller. Static
        # renders it with markup enabled by default.
        super().__init__(markup)
        self.add_class("message", f"message--{role}")
        # Fixed labels are safe as border chrome. Conversation and quiet metadata
        # roles map to "" and intentionally receive no title.
        label = _ROLE_LABELS.get(role, "")
        if label:
            self.border_title = label


class ToolCard(Static):
    """One evolving transcript card for a single tool call, keyed by call_id.

    A tool call emits up to three events sharing a call_id — request, an optional
    approval resolution (only for safety-gated tools), and a result. Rather than
    mint a separate line per event, one ``ToolCard`` is mounted on the request and
    then *mutated in place* as the later events arrive. The card carries its status
    in a semantically colored glyph plus the role CSS class (which styles its rail),
    so the whole lifecycle reads as one card transitioning pending → running →
    done/error instead of three stacked cards the reader has to reconcile. Resolved
    cards add a bounded multiline output preview below their compact status row.

    Parallel calls each own a stable card regardless of finish order, because the
    registry (in ``TextualTui``) routes every event to the card for its call_id.
    """

    BINDING_GROUP_TITLE = "Tool result"
    HELP = """
    # Tool result

    Review one tool request and its bounded result. Enter or Space expands extra
    output when available; expansion changes presentation only and never reruns the
    tool. Escape returns focus to the prompt or active safety decision.
    """

    # status → (leading glyph, role class). The role class drives the rail via the
    # shared `.message--{role}` CSS in TextualTui; glyph color is applied separately.
    #
    # denied and error previously shared both the "✗" glyph AND the "denied"
    # role class, making a user-denied tool call visually identical to a
    # genuine execution failure (issue #76). denied now gets its own glyph
    # ("⊘", already used for cancelled — both mean "stopped by a decision,
    # not a failure") and error gets its own "error" role class (CSS already
    # defines `.message--error`, it just was never applied here) — denied and
    # error are now distinguishable by glyph, label (_ROLE_LABELS below), and
    # color, not by color alone.
    _STATUS: dict[str, tuple[str, str]] = {
        "pending": ("⋯", "tool"),
        "denied": ("⊘", "denied"),
        "error": ("✗", "error"),
        "cancelled": ("⊘", "denied"),
        "done": ("✓", "approved"),
    }
    _GLYPH_STYLES: dict[str, str] = {
        "tool": "$text-accent",
        "approved": "$text-success",
        "denied": "$text-warning",
        "error": "$text-error",
    }

    # Border-title override for statuses whose role class (above) is shared
    # with another status that must read differently — "cancelled" reuses
    # "denied"'s role (same color/glyph family), but a cancelled tool call
    # was never actually denied, so its title must not say "denied". Statuses
    # not listed here fall back to _ROLE_LABELS keyed by role, as normal.
    _STATUS_LABELS: dict[str, str] = {
        "cancelled": "cancelled",
    }
    _TICK = 1.0  # the running counter only needs whole-second granularity

    # A resolved card is a keyboard target so the reader can expand its full output.
    # Cards without expandable content still take focus (harmless on a one-liner) but
    # their toggle is a no-op — simpler than making focusability conditional, which
    # Textual evaluates statically at mount.
    can_focus = True
    BINDINGS = [
        Binding("enter", "toggle_expand", "Expand/collapse", show=False),
        Binding("space", "toggle_expand", "Expand/collapse", show=False),
        Binding("escape", "leave", "Back to input", show=False),
    ]

    def __init__(
        self,
        name: str,
        arguments: object,
        *,
        arguments_available: bool = True,
    ) -> None:
        super().__init__("")
        # Not `_name`: Textual's DOMNode uses `self._name` to back the widget
        # `name` property (typed str | None), so a distinct field avoids
        # shadowing it and keeps this a plain str.
        self._tool_name = name
        self._call_arguments = (
            format_tool_call_arguments(name, arguments) if arguments_available else Content("")
        )
        # A plain str is untrusted output escaped at repaint; a Content is an
        # already-styled renderable (e.g. a colored diff) whose text is literal,
        # so it is composed directly without markup escaping.
        self._detail: str | Content | DiffPresentation = ""
        # The full (tool-bounded) output, kept so the reader can expand past the
        # collapsed preview/summary/diff. Untrusted text, rendered literally like a
        # str detail. Empty when there is nothing more to show than the detail.
        self._full_output: str = ""
        self._expanded = False
        # Whether the tool capped its own output; drives the honest "truncated at the
        # tool's limit" marker on the expanded view.
        self._truncated = False
        self._role = ""
        self._glyph = "⋯"
        # While running, `_elapsed` is a live whole-second tick count (looks alive,
        # exact precision doesn't matter mid-flight). On resolve it's replaced by
        # the true wall-clock duration derived from event timestamps (see
        # `set_state(elapsed=…)`), so the number that rests on screen is honest.
        self._elapsed: float | None = None
        self._timer: Timer | None = None
        self.set_state("pending")

    def on_mount(self) -> None:
        # A pending card ticks a running counter; a card that mounts already
        # resolved (e.g. rebuilt from history) has no timer to start.
        if self._role == "tool":
            self._elapsed = 0.0
            self._timer = self.set_interval(self._TICK, self._tick)
            self._repaint()

    def on_resize(self, event: events.Resize) -> None:
        """Repaint structured rows when a terminal resize changes source width."""

        if isinstance(self._detail, DiffPresentation):
            self._repaint()

    def update_call(self, name: str, arguments: object) -> None:
        """Enrich a historical result when its paged-in call arrives later."""

        self._tool_name = name
        self._call_arguments = format_tool_call_arguments(name, arguments)
        self._repaint()

    def on_unmount(self) -> None:
        self._stop_timer()

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _tick(self) -> None:
        previous = self._elapsed or 0.0
        self._elapsed = previous + self._TICK
        self._repaint(
            layout=len(_format_duration(previous)) != len(_format_duration(self._elapsed))
        )

    def set_state(
        self,
        status: str,
        *,
        detail: str | Content | DiffPresentation = "",
        elapsed: float | None = None,
        full_output: str = "",
        truncated: bool = False,
    ) -> None:
        """Transition the card to a new status, swapping glyph, color, and detail.

        ``detail`` adds a denial reason or bounded result preview below the persistent
        call header. A plain ``str`` is untrusted output escaped at
        repaint; a Textual ``Content`` is a pre-styled renderable (e.g. a colored
        diff) composed directly. ``elapsed`` is the true wall-clock duration (from
        the request/result event timestamps); passing it freezes the live counter
        at the honest value and stops the per-card timer. ``full_output`` is the
        tool's full (tool-bounded) output, retained so the reader can expand past the
        collapsed detail; ``truncated`` says the tool itself capped that output. The
        role CSS class is swapped rather than added so the rail reflects only the
        current state.
        """

        glyph, role = self._STATUS.get(status, self._STATUS["pending"])
        self._glyph = glyph
        if _has_detail(detail):
            self._detail = detail
        if full_output:
            self._full_output = full_output
        self._truncated = truncated
        if status != "pending":
            # Any terminal state (done/error/denied/cancelled) ends the call: stop
            # the live counter so a resolved card can never keep ticking. Freeze at
            # the true wall-clock duration when we have it; otherwise leave the last
            # ticked value (e.g. a cancel with no result timestamp to diff against).
            if elapsed is not None:
                self._elapsed = elapsed
            self._stop_timer()
        if role != self._role:
            if self._role:
                self.remove_class(f"message--{self._role}")
            self.add_class("message", f"message--{role}")
            self._role = role
        self.border_title = self._STATUS_LABELS.get(status, _ROLE_LABELS.get(role, "tool"))
        self._repaint()
        self.refresh_bindings()

    def _can_expand(self) -> bool:
        """Whether expanding would show anything the collapsed detail doesn't.

        True only when there is full output AND it differs from what the collapsed
        detail already shows — a short output whose preview is the whole thing, or a
        card with no retained output (pending, denied, error message), has nothing to
        expand, so its toggle is a no-op and no affordance is shown.
        """

        if isinstance(self._detail, DiffPresentation):
            return self._detail.can_expand
        if not self._full_output:
            return False
        detail_text = self._detail.plain if isinstance(self._detail, Content) else self._detail
        return self._full_output.strip() != detail_text.strip()

    def action_toggle_expand(self) -> None:
        """Expand or collapse the full output (Enter/Space on a focused card).

        Named ``toggle_expand`` rather than ``toggle`` because Textual's DOMNode
        already defines an ``action_toggle`` (for reactive attributes) with a
        different signature.
        """

        if not self._can_expand():
            return
        self._expanded = not self._expanded
        self._repaint()
        # A followed transcript should stay pinned to the tail when the *newest* card
        # grows; the app decides using the follow intent captured at focus and this
        # card's position (expanding a historical card must not yank the viewport).
        self.post_message(self.Toggled(self))

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "toggle_expand":
            return self._can_expand()
        return True

    def action_leave(self) -> None:
        """Return focus to the prompt input (Escape on a focused card)."""

        self.post_message(self.LeaveRequested())

    class Toggled(Message):
        """A card expanded or collapsed; the transcript may need to re-pin its tail.

        Carries the card so the app can re-pin only when the *newest* card grew —
        expanding an older card leaves the viewport alone so its content stays in view.
        """

        def __init__(self, card: ToolCard) -> None:
            super().__init__()
            self.card = card

    class LeaveRequested(Message):
        """A focused card asked to hand focus back to the prompt input."""

    def _repaint(self, *, layout: bool = True) -> None:
        # Build the whole card as Content, appending every untrusted value
        # (name, call arguments, detail) as LITERAL styled text. Nothing untrusted is
        # ever routed through a markup parser, so no escaping is needed and no
        # content — however it is truncated or whatever brackets it contains —
        # can inject or break a style span. Trusted chrome is literal too; only the
        # lifecycle glyph gets semantic color, applied out-of-band.
        glyph_style = self._GLYPH_STYLES.get(self._role, "$text-accent")
        content = (
            Content.styled(self._glyph, glyph_style)
            + Content(" ")
            + Content.styled(self._tool_name, "b")
        )
        if self._call_arguments.plain:
            content += Content("  ") + self._call_arguments
        if self._elapsed is not None:
            content += Content(f" · {_format_duration(self._elapsed)}")
        # Label the affordance so a reader does not have to infer what a bare
        # triangle means. Enter is the primary binding; Space remains supported.
        if self._can_expand():
            label = " ▾ less (Enter)" if self._expanded else " ▸ more (Enter)"
            content += Content(label)

        if isinstance(self._detail, DiffPresentation):
            # Structured edit/write cards retain diff rows for both states; unlike
            # generic tools, expansion must never replace review evidence with the
            # raw "Applied" or "Wrote" acknowledgement kept in _full_output.
            content += Content("\n") + _render_diff_presentation(
                self._detail,
                width=max(12, self.content_size.width or self.size.width or 80),
                expanded=self._expanded,
            )
        elif self._expanded and self._full_output:
            # Expanded: show the full (tool-bounded) output in place of the collapsed
            # detail, so the reader sees what the preview/summary stood in for.
            content += Content("\n") + self._indent_str(self._full_output)
        elif isinstance(self._detail, Content):
            # A pre-styled renderable is composed directly, preserving literal text.
            content += Content("\n") + _indent_content(self._detail)
        elif self._detail:
            content += Content("\n") + self._indent_str(self._detail)

        if self._truncated:
            # The tool capped its own output before it ever reached here, so what the
            # card shows — collapsed preview or expanded full output — isn't the whole
            # story. Say so honestly regardless of expand state: a capped output that
            # fits the preview budget (so there's nothing extra to expand) would
            # otherwise present as complete, which is exactly the case this marks.
            content += Content("\n  ⋯ output truncated at the tool's limit")

        self.update(content, layout=layout)

    @staticmethod
    def _indent_str(text: str) -> Content:
        """Indent untrusted output as literal text that inherits the card color."""

        indented = "\n".join(f"  {line}" for line in text.split("\n"))
        return Content(indented)


class WorkingIndicator(Static):
    """Transient heartbeat shown in the *transcript*, not the footer.

    Opencode-style: right after the user's prompt, a dim row
    ``⠋ Working… · 3s`` (or ``Retrying openai · 1/3 …``) that ticks alive
    and auto-removes when token streaming or a ToolCard mounts. Keeps the
    footer stable (cwd / session / model) — quiet over noisy.
    """

    _FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    _INTERVAL = 0.08  # ~12.5 fps braille rotation

    def __init__(self) -> None:
        super().__init__("", markup=False)
        self.add_class("message", "message--dim")
        self._ticks = 0
        self._label = "Working…"
        self._show_elapsed = True
        self._timer: Timer | None = None
        self._rendered_width: int | None = None

    def on_mount(self) -> None:
        self._start_timer()
        self._repaint()

    def on_unmount(self) -> None:
        self._stop_timer()

    def _tick(self) -> None:
        self._ticks += 1
        self._repaint()

    def restart_working(self) -> None:
        self._stop_timer()
        self._ticks = 0
        self._label = "Working…"
        self._show_elapsed = True
        self._start_timer()
        self._repaint()

    def show_working(self) -> None:
        if self._timer is None:
            self._start_timer()
        self._label = "Working…"
        self._show_elapsed = True
        self._repaint()

    def show_retry(self, label: str) -> None:
        if self._timer is None:
            self._start_timer()
        self._label = label
        self._show_elapsed = False
        self._repaint()

    def _start_timer(self) -> None:
        if self._timer is None:
            self._timer = self.set_interval(self._INTERVAL, self._tick)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _repaint(self) -> None:
        spinner = self._FRAMES[self._ticks % len(self._FRAMES)]
        text = f"{spinner} {self._label}"
        if self._show_elapsed:
            seconds = int(self._ticks * self._INTERVAL)
            text += f" · {seconds}s"
        # No Rich markup — this Static is markup=False and styled dim via
        # CSS class `message--dim` (muted, no border). Just plain text.
        width = len(text)
        self.update(text, layout=width != self._rendered_width)
        self._rendered_width = width


class StatusBar(Static):
    """Stable footer — cwd / session / model. No spinner, no transient state.

    The working heartbeat now lives in the transcript as ``WorkingIndicator``
    (opencode-style), so the footer stays calm and persistent.
    """

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 - Textual API
        super().__init__("idle", id=id, markup=False)
        self._snapshot = TuiViewSnapshot(status="idle", input_hint="wisp> ")

    def on_mount(self) -> None:
        self._render_status()

    def on_resize(self, event: events.Resize) -> None:
        self._render_status()

    def set_snapshot(self, snapshot: TuiViewSnapshot) -> None:
        self._snapshot = snapshot
        self._render_status()

    def _render_status(self) -> None:
        width = self.content_size.width
        self.update(format_tui_footer_text(self._snapshot, width=width if width > 0 else None))


class _StreamMarkdown(Markdown):
    """Markdown child whose mount-time empty update has definitely completed."""

    def __init__(self) -> None:
        super().__init__()
        self._ready = asyncio.Event()

    # Textual's runtime Markdown handler is async although its inherited type is sync.
    async def _on_mount(self, event: events.Mount) -> None:  # type: ignore[override]
        # Textual 8.2.8 dispatches named handlers for every class in the widget MRO.
        # Prevent that default traversal because we must signal readiness *after*
        # Markdown's mount-time update("") and Widget's mount setup both finish.
        # Calling super() without this guard would run Markdown._on_mount twice.
        event.prevent_default()
        try:
            await super()._on_mount(event)
            Widget._on_mount(self, event)
        finally:
            self._ready.set()

    async def wait_until_ready(self) -> None:
        """Wait until Textual can no longer overwrite content during mounting."""

        await self._ready.wait()


class StreamMessage(Widget):
    """One assistant turn rendered through Textual's public Markdown API."""

    DEFAULT_CSS = """
    StreamMessage {
        height: auto;
    }

    StreamMessage > Markdown,
    StreamMessage > .stream-fallback {
        height: auto;
        margin: 0;
    }

    StreamMessage > Markdown {
        padding: 0;
        color: $text;
    }

    StreamMessage MarkdownHeader {
        width: 1fr;
        content-align: left middle;
        margin: 1 0 0 0;
    }

    StreamMessage MarkdownBlock,
    StreamMessage MarkdownTableCellContents {
        link-color: $text-primary;
        link-color-hover: $text-accent;
        link-style: underline;
        link-style-hover: bold underline;
    }

    StreamMessage MarkdownBlock:dark > .code_inline,
    StreamMessage MarkdownBlock:light > .code_inline {
        color: $text-accent;
        background: $panel;
    }

    StreamMessage MarkdownFence:dark,
    StreamMessage MarkdownFence:light {
        color: $text;
        background: $panel;
        border-left: outer $secondary;
        padding: 0 1;
        margin: 1 0;
    }

    StreamMessage MarkdownFence > Label {
        padding: 0 1;
    }

    StreamMessage MarkdownFence:ansi {
        color: $text;
        background: transparent;
        border-left: none;
        padding: 0;
        margin: 0;
    }

    StreamMessage MarkdownFence:ansi > Label {
        padding: 1 0;
    }

    StreamMessage MarkdownBlockQuote:dark,
    StreamMessage MarkdownBlockQuote:light {
        color: $text-muted;
        background: transparent;
        border-left: outer $secondary;
        padding: 0 1;
        margin: 1 0;
    }

    StreamMessage MarkdownBlockQuote MarkdownParagraph {
        margin: 0 0 1 0;
    }

    StreamMessage MarkdownBlockQuote MarkdownParagraph:last-child {
        margin-bottom: 0;
    }

    StreamMessage MarkdownBullet:dark,
    StreamMessage MarkdownBullet:light {
        color: $accent;
    }

    StreamMessage MarkdownHorizontalRule {
        height: 1;
        padding-top: 0;
        margin: 1 0;
        border-bottom: solid $secondary;
    }

    StreamMessage MarkdownTableContent > .header {
        color: $text-primary;
    }

    StreamMessage Markdown > MarkdownHeader:first-child,
    StreamMessage Markdown > MarkdownFence:first-child,
    StreamMessage Markdown > MarkdownBlockQuote:first-child,
    StreamMessage Markdown > MarkdownHorizontalRule:first-child {
        margin-top: 0;
    }

    StreamMessage Markdown > MarkdownParagraph:last-child,
    StreamMessage Markdown > MarkdownList:last-child,
    StreamMessage Markdown > MarkdownFence:last-child,
    StreamMessage Markdown > MarkdownBlockQuote:last-child,
    StreamMessage Markdown > MarkdownTable:last-child,
    StreamMessage Markdown > MarkdownHorizontalRule:last-child {
        margin-bottom: 0;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.add_class("message", "message--assistant")
        # Match settled assistant turns: role styling remains, but conversation
        # cards intentionally have no visible role title.
        self._markdown = _StreamMarkdown()
        # Keep the literal fallback mounted but hidden. If Markdown's authoritative
        # replacement fails, switching already-mounted children is synchronous and
        # preserves this StreamMessage's history identity.
        self._fallback = Static(Content(), classes="stream-fallback")
        self._fallback.display = False

    def compose(self) -> ComposeResult:
        yield self._markdown
        yield self._fallback

    async def append_markdown(self, fragment: str) -> None:
        """Append one provider fragment after Markdown mount initialization."""

        await self._markdown.wait_until_ready()
        await self._markdown.append(fragment)

    async def replace_markdown(self, content: str) -> None:
        """Render authoritative content, falling back to literal text on failure."""

        await self._markdown.wait_until_ready()
        try:
            await self._markdown.update(content)
        except Exception as error:
            # MessageCompleted is suppressed after streaming starts, making this
            # authoritative source the only live copy. Preserve it as literal text
            # rather than allowing a parser/layout failure to leave a blank turn.
            self.log.error(f"Markdown finalization failed; using literal fallback: {error}")
            self._fallback.update(Content(content))
            self._markdown.display = False
            self._fallback.display = True
