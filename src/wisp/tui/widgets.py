"""Per-message transcript widgets for the Textual TUI.

Stage 2 replaces the append-only ``RichLog`` transcript with a
``VerticalScroll`` of these widgets, one per turn/event. Two kinds:

- ``LineMessage`` — a role-styled single block for tool calls, results,
  approvals, errors, notices, and user input. Content is escaped Rich markup in
  a ``Static`` (never fed to the Markdown parser), preserving the
  escape-at-boundary invariant for untrusted tool/error payloads.
- ``StreamMessage`` — the streaming assistant turn, backed by one ``Static``
  whose Rich Markdown renderable preserves structure without mounting a nested
  Textual widget for every block.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import ClassVar, Protocol, cast

from rich.cells import cell_len
from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.markdown import CodeBlock, Heading
from rich.markdown import Markdown as RichMarkdown
from rich.segment import Segment
from rich.style import Style as RichStyle
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme as RichTheme
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.css.styles import RulesMap
from textual.geometry import Size
from textual.message import Message
from textual.selection import Selection
from textual.strip import Strip
from textual.style import Style
from textual.visual import RenderOptions, RichVisual
from textual.widget import AwaitMount, Widget
from textual.widgets import (
    DataTable,
    Label,
    LoadingIndicator,
    OptionList,
    RadioButton,
    RadioSet,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option

from wisp.coding.costs import format_cost_summary
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
    _DecisionRole,
    _trust_content,
)
from wisp.tui.diff_presentation import (
    DIFF_ADD_COUNT_STYLE,
    DIFF_DEL_COUNT_STYLE,
    DiffPresentation,
)
from wisp.tui.diff_rendering import render_diff_visible_row as _render_diff_visible_row
from wisp.tui.file_index import ProjectSnapshot
from wisp.tui.file_result_presentation import (
    FileResultPresentation,
    render_file_result_presentation,
)
from wisp.tui.incremental_markdown import IncrementalMarkdownState
from wisp.tui.input_types import (
    PendingSubmissionView,
    QueueSubmissionKind,
    pending_submission_preview_lines,
)
from wisp.tui.overlay import TranscriptViewportState
from wisp.tui.presentation_clock import PresentationClock
from wisp.tui.process_lifecycle import ProcessLifecyclePresentation
from wisp.tui.prompt_highlighting import (
    PromptHighlight,
    PromptHighlightKind,
    prompt_document_highlights,
)
from wisp.tui.rendering import (
    TuiViewSnapshot,
    _footer_context_text,
    _format_cwd_for_footer,
    _sanitize_footer_text,
    _truncate_to_cell_width,
)
from wisp.tui.tool_call import (
    ToolActionStatus,
    _format_tool_call_action_from_rendered,
    format_tool_call_arguments,
)

_TOOL_OUTPUT_PREVIEW_LINES = 8
_TOOL_OUTPUT_PREVIEW_BYTES = 2_000
PASTE_DISPLAY_THRESHOLD = 2_000
_PROCESS_ID_DISPLAY_CELLS = 40
_MARKDOWN_FENCE_RE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$")
_PROMPT_HIGHLIGHT_COMPONENTS: dict[PromptHighlightKind, str] = {
    "command": "prompt-editor--command",
    "resolved_path": "prompt-editor--resolved-path",
    "unresolved_path": "prompt-editor--unresolved-path",
    "markdown_heading": "prompt-editor--markdown-heading",
    "markdown_list_marker": "prompt-editor--markdown-list-marker",
    "markdown_inline_code_delimiter": "prompt-editor--markdown-inline-code-delimiter",
    "markdown_inline_code": "prompt-editor--markdown-inline-code",
    "markdown_fence_delimiter": "prompt-editor--markdown-fence-delimiter",
    "markdown_fence_info": "prompt-editor--markdown-fence-info",
    "markdown_fence_body": "prompt-editor--markdown-fence-body",
}


class PromptEditor(TextArea):
    """Multiline prompt editor with bounded, catalog-backed semantic styling."""

    DEFAULT_CSS = """
    PromptEditor {
        & .prompt-editor--command {
            color: $accent;
            text-style: bold;
        }
        & .prompt-editor--resolved-path {
            color: $success;
            text-style: underline;
        }
        & .prompt-editor--unresolved-path {
            color: $warning;
            text-style: dim underline;
        }
        & .prompt-editor--markdown-heading {
            color: $warning;
            text-style: bold;
        }
        & .prompt-editor--markdown-list-marker {
            color: $accent;
            text-style: bold;
        }
        & .prompt-editor--markdown-inline-code-delimiter {
            color: $secondary;
            text-style: dim;
        }
        & .prompt-editor--markdown-inline-code {
            color: $accent;
        }
        & .prompt-editor--markdown-fence-delimiter {
            color: $secondary;
            text-style: bold;
        }
        & .prompt-editor--markdown-fence-info {
            color: $accent;
            text-style: italic;
        }
        & .prompt-editor--markdown-fence-body {
            color: $secondary;
        }
    }
    """
    COMPONENT_CLASSES: ClassVar[set[str]] = TextArea.COMPONENT_CLASSES | set(
        _PROMPT_HIGHLIGHT_COMPONENTS.values()
    )

    BINDING_GROUP_TITLE = "Prompt editor"
    HELP = """
    # Prompt editor

    Write a prompt and press **Enter** to send it. While Wisp is running, Enter steers
    the active run and **Alt+Enter** queues a follow-up; **Alt+Up** restores the latest
    queued item. Use **Shift+Enter** or **Ctrl+J** for a newline. Type `/` for commands
    or `@` to reference a project path; when a suggestion menu is visible, Enter accepts
    its highlighted item.
    Tool approval panels default to **1 (Approve once)**; their own contextual help
    explains every permission scope before you decide.
    """
    BINDINGS = [
        Binding("enter", "submit", "Send / accept suggestion", show=False),
        Binding("shift+enter,ctrl+j", "newline", "Newline", show=False),
        Binding("alt+enter", "alternate_submit", "Follow up / newline", show=False),
        Binding("alt+up", "restore_queued", "Restore queued item", show=False),
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

        def __init__(
            self,
            value: str,
            display: str,
            queue_kind: QueueSubmissionKind = "auto",
        ) -> None:
            super().__init__()
            self.value = value
            self.display = display
            self.queue_kind = queue_kind

    class RestoreQueued(Message):
        """Request restoration of the newest eligible runtime-queued item."""

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
        self._command_tokens: frozenset[str] = frozenset()
        self._project_paths: frozenset[str] | None = None
        self._unresolved_paths_known = False
        self._semantic_highlight_revision = 0
        self._cached_semantic_highlight_revision = -1
        self._semantic_highlights: tuple[tuple[PromptHighlight, ...], ...] = ()
        self._agent_running = False

    def set_running(self, running: bool) -> None:
        """Resolve mode-sensitive submit bindings from the latest shell snapshot."""

        self._agent_running = running

    def set_command_catalog(self, catalog: TuiCommandCatalog) -> None:
        """Replace recognized runtime and Textual-local slash spellings."""

        tokens = frozenset(
            token
            for descriptor in catalog.descriptors
            for token in (descriptor.slash_command, *descriptor.slash_aliases)
            if token.startswith("/")
        )
        if tokens != self._command_tokens:
            self._command_tokens = tokens
            self._refresh_semantic_styles()

    def set_project_snapshot(self, snapshot: ProjectSnapshot | None) -> None:
        """Replace the complete bounded path snapshot used for resolution."""

        paths = frozenset(snapshot.paths) if snapshot is not None and snapshot.entries else None
        unresolved_paths_known = (
            snapshot is not None and bool(snapshot.entries) and not snapshot.truncated
        )
        if (paths, unresolved_paths_known) != (
            self._project_paths,
            self._unresolved_paths_known,
        ):
            self._project_paths = paths
            self._unresolved_paths_known = unresolved_paths_known
            self._refresh_semantic_styles()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Invalidate document-level Markdown state after every editor mutation."""

        if event.text_area is self:
            self._semantic_highlight_revision += 1
            # One fence edit can restyle every later visible line. TextArea has
            # already updated layout and its changed rows, so request only a
            # repaint rather than another global style-cache invalidation.
            self.refresh()

    def _refresh_semantic_styles(self) -> None:
        """Invalidate TextArea caches and schedule a visible repaint."""

        self._semantic_highlight_revision += 1
        self.notify_style_update()
        self.refresh()

    def _highlights_for_line(self, line_index: int) -> tuple[PromptHighlight, ...]:
        if self._cached_semantic_highlight_revision != self._semantic_highlight_revision:
            self._semantic_highlights = prompt_document_highlights(
                self.document.lines,
                command_tokens=self._command_tokens,
                project_paths=self._project_paths,
                unresolved_paths_known=self._unresolved_paths_known,
            )
            self._cached_semantic_highlight_revision = self._semantic_highlight_revision
        if line_index >= len(self._semantic_highlights):
            return ()
        return self._semantic_highlights[line_index]

    def get_line(self, line_index: int) -> Text:
        """Apply presentation-only spans through TextArea's public styling hook."""

        rendered = super().get_line(line_index)
        for highlight in self._highlights_for_line(line_index):
            rendered.stylize(
                self.get_component_rich_style(
                    _PROMPT_HIGHLIGHT_COMPONENTS[highlight.kind],
                    partial=True,
                ),
                highlight.start,
                highlight.end,
            )
        return rendered

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
        elif event.key == "alt+enter":
            self.action_alternate_submit()
            event.stop()
            event.prevent_default()
        elif event.key == "alt+up":
            self.action_restore_queued()
            event.stop()
            event.prevent_default()

    def action_submit(self) -> None:
        """Submit a prompt or steering message with compact display metadata."""

        kind: QueueSubmissionKind = "steering" if self._agent_running else "auto"
        self.post_message(self.Submitted(self.text_for_submission(), self.text, kind))

    def action_alternate_submit(self) -> None:
        """Queue a follow-up while running; otherwise preserve Alt+Enter newline."""

        if not self._agent_running:
            self.action_newline()
            return
        self.post_message(self.Submitted(self.text_for_submission(), self.text, "follow_up"))

    def action_restore_queued(self) -> None:
        """Ask the shell to restore the newest eligible active-run queue item."""

        # RPC queue mutation is active-run-only. Retained items remain visible and
        # carry into the next run rather than being cleared by frontend policy.
        if self._agent_running:
            self.post_message(self.RestoreQueued())

    def action_newline(self) -> None:
        self.insert("\n")


class _PresentationClockApp(Protocol):
    """Narrow app surface used by widgets sharing the presentation clock."""

    presentation_clock: PresentationClock


def _presentation_clock(widget: Widget) -> PresentationClock:
    """Return the app-owned presentation clock for a mounted Wisp widget."""

    return cast(_PresentationClockApp, widget.app).presentation_clock


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


def _has_detail(detail: str | Content | DiffPresentation | FileResultPresentation) -> bool:
    """Whether a card detail carries content, for both str and Content forms.

    An empty ``str`` and an empty ``Content`` both mean "no detail", so a
    degenerate render never overwrites a real prior detail or forces an empty
    block. A ``Content`` is truthy by identity, so check its text explicitly.
    """

    if isinstance(detail, Content):
        return bool(detail.plain)
    if isinstance(detail, DiffPresentation):
        return bool(detail.rows)
    if isinstance(detail, FileResultPresentation):
        return bool(detail.summary)
    return bool(detail)


def _tree_line(
    value: Content,
    *,
    width: int,
    first_prefix: str,
    continuation_prefix: str,
) -> Content:
    """Wrap one literal line with stable tree prefixes and hanging indentation."""

    available = max(1, width - max(cell_len(first_prefix), cell_len(continuation_prefix)))
    # Content.wrap() performs word-boundary normalization even when no wrapping is
    # needed, which can consume a literal separator between differently styled spans
    # (for example ``Ran `` + a muted command becoming ``Ranpytest`` on screen).
    # Preserve the original styled content on the overwhelmingly common fitting path.
    wrapped = (
        [value]
        if cell_len(value.plain) <= available
        else value.wrap(available, overflow="fold") or [Content("")]
    )
    if len(wrapped) == 1:
        return Content(first_prefix) + wrapped[0]
    return Content(first_prefix) + Content("\n" + continuation_prefix).join(wrapped)


def _tree_detail(detail: str | Content, *, width: int) -> Content:
    """Render literal result text beneath a tool action using one tree branch."""

    source = detail if isinstance(detail, Content) else Content(detail)
    logical_lines = source.split("\n", allow_blank=True) or [Content("")]
    # ``Content.__add__`` copies the accumulated text and every span on each call,
    # so folding hundreds of output lines one at a time is quadratic — long process
    # output (a pytest progress dump) took seconds to lay out. ``join`` accumulates
    # into lists and builds the result once.
    return Content("\n").join(
        _tree_line(
            logical_line,
            width=width,
            first_prefix="  └ " if index == 0 else "    ",
            continuation_prefix="    ",
        )
        for index, logical_line in enumerate(logical_lines)
    )


def _file_result_detail(
    presentation: FileResultPresentation,
    *,
    width: int,
    expanded: bool,
) -> Content:
    """Render a file result with its toggle aligned on the summary row."""

    rendered = render_file_result_presentation(
        presentation,
        width=max(12, width - 4),
        expanded=expanded,
    )
    if not presentation.can_expand:
        return rendered
    lines = rendered.split("\n", allow_blank=True) or [Content("")]
    label = "▾ collapse (Enter)" if expanded else f"▸ {presentation.expand_label} (Enter)"
    label_content = Content.styled(label, "$text-muted")
    available = max(1, width - 4)
    used = cell_len(lines[0].plain) + cell_len(label)
    if used + 2 <= available:
        lines[0] += Content(" " * (available - used)) + label_content
    else:
        lines.insert(1, label_content)
    return Content("\n").join(lines)


def _render_diff_presentation(
    presentation: DiffPresentation,
    *,
    width: int,
    expanded: bool,
) -> Content:
    """Paint one structured diff beneath the tool action's tree branch."""

    inner_width = max(1, width - 4)  # ``  └ `` / four-space continuation gutter.
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
    # Fold the rows with ``join`` rather than repeated ``+=``: ``Content.__add__``
    # copies the accumulated text and spans every time, so an expanded diff (up to
    # ``DIFF_EXPANDED_ROWS``) would lay out in quadratic time.
    rows = Content("\n  ").join(
        _render_diff_visible_row(
            visible_row,
            width=inner_width,
            show_line_numbers=presentation.show_line_numbers,
        )
        for visible_row in presentation.visible_rows(expanded=expanded)
    )
    content = Content("  └ ") + header
    if rows.plain:
        content += Content("\n  ") + rows
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
    approves); the trust prompt remains deny-first (Enter/Escape declines).
    Escape always denies on every panel, regardless of which option is highlighted.
    """

    BINDING_GROUP_TITLE = "Safety decision"
    HELP = """
    # Safety decision

    **Approve once** permits only this request. A tool-session choice permits the
    named tool until this Wisp process exits. **YOLO** permits every mutating and
    command tool for the process. Project trust permits loading that project's
    local settings, instructions, and skills. Escape always chooses the conservative deny result.
    """
    BINDINGS = [
        Binding("1", "choose(1)", "Choose option 1", show=False),
        Binding("2", "choose(2)", "Choose option 2", show=False),
        Binding("3", "choose(3)", "Choose option 3", show=False),
        Binding("4", "choose(4)", "Choose option 4", show=False),
        Binding("escape", "conservative_cancel", "Deny", show=False),
    ]

    DEFAULT_CSS = """
    DecisionPanel {
        display: none;
        width: 72;
        max-width: 90%;
        height: auto;
        max-height: 14;
        margin: 0 1;
        padding: 0 1;
        border: round $warning;
        background: $background;
    }

    DecisionPanel #decision-title {
        height: 1;
        color: $warning;
        text-style: bold;
    }

    DecisionPanel.decision--read {
        border: round $primary;
    }

    DecisionPanel.decision--read #decision-title {
        color: $primary;
    }

    DecisionPanel.decision--mutating {
        border: round $warning;
    }

    DecisionPanel.decision--mutating #decision-title {
        color: $warning;
    }

    DecisionPanel.decision--command {
        border: heavy $error;
    }

    DecisionPanel.decision--command #decision-title {
        color: $error;
    }

    DecisionPanel.decision--trust {
        border: double $accent;
    }

    DecisionPanel.decision--trust #decision-title {
        color: $accent;
    }

    DecisionPanel.decision--fallback {
        border: round $warning;
    }

    DecisionPanel.decision--fallback #decision-title {
        color: $warning;
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

    def navigation_key_changes_highlight(self, key: str) -> bool:
        """Whether a panel navigation key can select a different option."""

        highlighted = self._options.highlighted
        if highlighted is None:
            return False
        option_count = 4 if self._mode == "approval" else 2
        if key in {"up", "home", "pageup"}:
            return highlighted > 0
        if key in {"down", "end", "pagedown"}:
            return highlighted < option_count - 1
        return True

    def show_approval(self, event: ToolApprovalRequested, *, cwd: str) -> None:
        content = _approval_content(event, cwd=_format_cwd_for_footer(cwd))
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
        for role in _DecisionRole:
            self.remove_class(f"decision--{role.value}")
        self.add_class(f"decision--{content.role.value}")
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
            "trust": {1: "y", 2: "n"},
        }
        if answer := answers.get(self._mode, {}).get(number):
            self.submit_answer(answer)

    def action_conservative_cancel(self) -> None:
        self.submit_answer("n")


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
            self._options.add_option(Option(entry.display_name, disabled=True))
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

    Drawn lettering sits above the tagline, prompt hint and quick-action
    reminder, without consuming a permanent header row. Every direct child is
    given the same explicit width because Textual centers these siblings as a
    block rather than independently — the width therefore has to fit the widest
    of them, and the drawn wordmark is wider than any of the text lines.

    Deliberately carries no provider/model/project line: the footer already
    reports all three on the same screen, so repeating them here would be pure
    duplication rather than orientation.

    The panel sheds content as the viewport shrinks rather than overflowing:
    first the quick actions, then the tagline and hint, and finally the tall
    wordmark is exchanged for a one-row badge. That exchange also happens when
    the viewport is merely too *narrow* for the drawn mark, which wraps rather
    than clips and would otherwise arrive at several times its expected height.

    Which tier applies is decided by measuring each one's wrapped footprint,
    not by comparing height against fixed thresholds. Once the text rows wrap,
    their row count depends on the text, the available width and the tier
    itself, so no constant is correct at every width.

    The shared block width itself is also re-derived on every resize rather than
    fixed once at construction. `_MIN_BLOCK_WIDTH` is a comfortable width for the
    text lines, not a floor Textual will honor on a narrower terminal — pinning
    every child to it regardless of the real viewport clipped the tagline and
    hint mid-word instead of letting them wrap or shrink with the panel.
    """

    # A comfortable width for the text lines, used only when the viewport can
    # actually provide it. On a narrower terminal the available width governs
    # instead, so children track the real space rather than clipping against a
    # value the panel can no longer honor.
    _MIN_BLOCK_WIDTH = 40

    # Rows the drawn mark occupies, and the margin every child but the first
    # carries. Used to compute a tier's footprint, not to gate it directly.
    _CHILD_MARGIN = 1

    # Tiers from richest to sparsest. Each entry is the classes to apply; the
    # first whose measured footprint fits is used, so the panel always shows as
    # much as the viewport genuinely allows.
    _TIERS: tuple[frozenset[str], ...] = (
        frozenset(),
        frozenset({"-compact"}),
        frozenset({"-compact", "-minimal"}),
    )

    DEFAULT_CSS = """
    TranscriptEmptyState.-compact #transcript-empty-actions {
        display: none;
    }

    TranscriptEmptyState.-minimal #transcript-empty-tagline,
    TranscriptEmptyState.-minimal #transcript-empty-hint {
        display: none;
    }
    """

    def __init__(
        self,
        wordmark: str,
        compact_wordmark: str,
        tagline: str,
        hint: str,
    ) -> None:
        super().__init__(id="transcript-empty")
        self._wordmark = wordmark
        self._compact_wordmark = compact_wordmark
        self._tagline = tagline
        self._hint = hint
        self._wordmark_label: Static | None = None
        # Cells the drawn mark needs before Textual starts wrapping its rows.
        # Derived from the art itself so the fit check cannot drift from it.
        self._wordmark_width = max(
            (len(line) for line in wordmark.splitlines()),
            default=0,
        )
        self._actions = "/ commands  ·  /resume session"
        # Populated by compose(); every direct child gets the same width so
        # Textual's block-centering keeps them aligned with one another.
        self._centered_children: list[Widget] = []

    def _centered(self, widget: Widget) -> Widget:
        self._centered_children.append(widget)
        return widget

    def compose(self) -> ComposeResult:
        self._wordmark_label = Static(self._wordmark, id="transcript-empty-wordmark", markup=False)
        yield self._centered(self._wordmark_label)
        yield self._centered(Label(self._tagline, id="transcript-empty-tagline", markup=False))
        yield self._centered(Label(self._hint, id="transcript-empty-hint", markup=False))
        yield self._centered(Static(self._actions, id="transcript-empty-actions", markup=False))

    def _wrapped_rows(self, text: str, width: int) -> int:
        """Rows ``text`` occupies once wrapped into ``width`` cells.

        Delegates to Textual's own measurement rather than reimplementing its
        word wrapping. A hand-rolled version drifted immediately: splitting on
        whitespace collapsed the double spaces around the actions line's
        separators, so a string that genuinely wrapped was measured as fitting.
        """

        if width <= 0:
            return 0
        return Content(text).get_height(self.styles.get_rules(), width)

    def _tier_footprint(self, classes: frozenset[str], *, width: int, mark_rows: int) -> int:
        """Rows this tier needs at ``width``, including inter-child margins."""

        texts = [self._tagline, self._hint]
        if "-minimal" in classes:
            texts = []
        if "-compact" not in classes:
            texts.append(self._actions)
        rows = mark_rows
        for text in texts:
            rows += self._CHILD_MARGIN + self._wrapped_rows(text, width)
        return rows

    def on_resize(self, event: events.Resize) -> None:
        self._refresh_responsive_layout()

    def _refresh_responsive_layout(self) -> None:
        """Fit the active startup or ready copy to the current viewport."""

        height = self.size.height
        width = self.size.width

        # The drawn mark needs room on both axes, for different reasons. Too
        # short and it cannot fit its own rows. Too narrow and Textual wraps
        # each row instead of clipping it, which shears the letterforms apart
        # and multiplies its height.
        mark_rows = len(self._wordmark.splitlines())
        drawn_mark_fits = height >= mark_rows and width >= self._wordmark_width
        label = self._wordmark_label
        if label is not None:
            label.update(self._wordmark if drawn_mark_fits else self._compact_wordmark)

        # Whichever mark is showing is this panel's widest child, so it sets the
        # comfortable width; `_MIN_BLOCK_WIDTH` only raises that when there is
        # room to spare. Clamped to the real viewport so a narrow terminal
        # shrinks the whole block, rather than Textual clipping the tagline and
        # hint mid-word against a width the panel cannot actually honor.
        mark_width = self._wordmark_width if drawn_mark_fits else len(self._compact_wordmark)
        block_width = min(width, max(mark_width, self._MIN_BLOCK_WIDTH))
        for child in self._centered_children:
            child.styles.width = block_width

        # Shed tiers by MEASURING each one's wrapped footprint rather than
        # comparing height against fixed thresholds. Once the text rows wrap,
        # a row count is a function of the text, the available width and which
        # tier is showing — so any constant is wrong at some width. At twelve
        # cells the tagline alone occupies four rows, which a one-row-per-child
        # budget under-counts badly enough to overflow the panel.
        rows = mark_rows if drawn_mark_fits else 1
        for classes in self._TIERS:
            if self._tier_footprint(classes, width=block_width, mark_rows=rows) <= height:
                break
        for name in ("-compact", "-minimal"):
            self.set_class(name in classes, name)


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

    OperationIndicator.-covers-transcript {
        background: $background;
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
        margin-right: 2;
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
        self._spinner = LoadingIndicator(id="operation-indicator-spinner")

    def compose(self) -> ComposeResult:
        with Horizontal(id="operation-indicator-panel"):
            yield self._spinner
            yield self._label

    def on_mount(self) -> None:
        # `LoadingIndicator` arms a 16 Hz auto-refresh on its own mount and never
        # disarms it. This surface starts hidden (`display: none`), so that timer
        # would animate an invisible spinner for the whole session — and each tick
        # asks `is_on_screen`, a widget geometry lookup that rebuilds the compositor's
        # complete map across every mounted widget. Run the spinner only while shown.
        self._set_spinner_running(False)

    def _set_spinner_running(self, running: bool) -> None:
        """Animate the spinner only while the operation surface is visible."""

        self._spinner.auto_refresh = 1 / 16 if running else None

    @property
    def is_open(self) -> bool:
        """Whether the operation surface is currently visible."""

        return self.display

    def show_operation(self, label: str, *, cover_transcript: bool = False) -> None:
        """Display one operation and configure whether it covers partial history."""

        self.set_class(cover_transcript, "-covers-transcript")
        self.update_operation(label)
        self.display = True
        self._set_spinner_running(True)

    def update_operation(self, label: str) -> None:
        """Update an active operation label without changing spinner or coverage state."""

        self._label.update(label)

    def hide(self) -> None:
        """Hide the operation surface through the generic overlay protocol."""

        self.display = False
        self._set_spinner_running(False)


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


class HistoryNavigationIntent(Enum):
    """Reader intent carried across a retained-history window replacement."""

    PRESERVE = auto()
    PAGE_UP = auto()
    WHEEL_UP = auto()
    PAGE_DOWN = auto()
    WHEEL_DOWN = auto()
    OLDEST = auto()


@dataclass(frozen=True)
class HistoryNavigation:
    """Viewport movement left to apply after a history edge has mounted."""

    intent: HistoryNavigationIntent = HistoryNavigationIntent.PRESERVE
    remaining_rows: float = 0.0
    reader_generation: int = -1


class Transcript(VerticalScroll):
    """Scrollable message container that follows the newest output like `tail -f`.

    Auto-scroll is driven by a sticky ``_follow`` flag rather than a per-append
    "am I near the bottom?" measurement. That measurement is self-defeating while
    streaming: the growing content is what pushes the bottom away, so a snapshot
    taken as it grows reads "not at the bottom" and abandons following the very
    output it should track.

    Instead the flag tracks reader *intent*, updated only from a genuine reader-
    or app-driven movement (``watch_scroll_y``, guarded — see below):

    - Rest at the bottom → ``True`` (keep following new output).
    - The user scrolls up and away → ``False`` (they're reading history; don't
      yank them back). Scrolling back to the bottom flips it ``True`` again.

    ``scroll_y`` itself can also move with no reader input: mounting or removing
    widgets changes ``max_scroll_y``, and Textual re-validates ``scroll_y``
    against it (``_size_updated`` → ``_scroll_update``), which can *clamp* a
    reader's parked position down — landing it exactly on the new
    ``max_scroll_y`` and satisfying "at the bottom" by coincidence, not intent.
    ``_size_updated`` marks that window as content-driven so ``watch_scroll_y``
    never reads it as the reader returning to the tail. After each append the
    app calls ``follow_tail()``, which scrolls to the end iff the flag is set —
    that programmatic scroll is real movement and re-derives to ``True`` as
    expected.
    """

    BINDING_GROUP_TITLE = "Conversation"
    HELP = """
    # Conversation

    Page through the transcript with PageUp and PageDown; older history loads as
    needed. Home reaches the beginning of the session and End returns to live output.
    The scrollbar represents the mounted history window. Wisp follows new output
    only while the viewport is resting at the latest message.
    """

    class FollowChanged(Message):
        """The viewport entered or left sticky tail-follow mode."""

        def __init__(self, following: bool) -> None:
            super().__init__()
            self.following = following

    class NeedMoreHistory(Message):
        """The user reached the oldest mounted transcript content."""

        def __init__(self, navigation: HistoryNavigation) -> None:
            super().__init__()
            self.navigation = navigation

    class NeedNewerHistory(Message):
        """A forward reader gesture crossed the newest mounted content."""

        def __init__(self, navigation: HistoryNavigation) -> None:
            super().__init__()
            self.navigation = navigation

    def __init__(
        self,
        *args: object,
        empty_wordmark: str | None = None,
        empty_compact_wordmark: str = "",
        empty_tagline: str = "",
        empty_hint: str = "",
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._follow = True
        self._follow_generation = 0
        self._content_driven_scroll_update = False
        self._empty_wordmark = empty_wordmark
        self._empty_compact_wordmark = empty_compact_wordmark
        self._empty_tagline = empty_tagline
        self._empty_hint = empty_hint
        self._empty_state: TranscriptEmptyState | None = None
        self._has_more_history = False
        self._has_retained_history = False
        self._has_newer_history = False
        self._history_loading = False
        self._history_request_armed = True
        self._history_navigation = HistoryNavigation()
        self._pending_newer_navigation = HistoryNavigation()
        self._newer_navigation_scheduled = False

    def _size_updated(
        self,
        size: Size,
        virtual_size: Size,
        container_size: Size,
        layout: bool = True,
    ) -> bool:
        """Apply measured scroll geometry without requesting a second layout.

        A virtual-size change (mount/remove) re-validates ``scroll_y`` against the
        new ``max_scroll_y`` here, which can *move* ``scroll_y`` with no reader
        input at all — e.g. clamping a reader's parked position down when the
        mounted window shrinks. That lands ``watch_scroll_y`` a value change to
        react to, so mark the call as content-driven for its duration: it must
        never be read as the reader returning to the tail.
        """

        self._content_driven_scroll_update = True
        try:
            changed = super()._size_updated(size, virtual_size, container_size, layout=False)
        finally:
            self._content_driven_scroll_update = False
        if changed:
            self._clamp_scroll_target_to_content()
        return changed

    def _clamp_scroll_target_to_content(self) -> None:
        """Discard a scroll target that shrinking content left beyond the last row.

        Textual steps the wheel from ``scroll_target_y`` rather than ``scroll_y``, and
        re-validates only ``scroll_y`` when content shrinks. A target left above the
        new ``max_scroll_y`` therefore silently absorbs every wheel event: each one
        subtracts a step from the stale target, still clamps back to the same row, and
        reports that nothing scrolled — so the transcript appears frozen until the
        accumulated overshoot is spent.

        Content shrinking under a parked viewport is routine here (a process card
        collapsing captured output is enough), so re-clamp the target whenever the
        measured geometry can no longer reach it.
        """

        max_scroll_y = self.max_scroll_y
        if self.scroll_target_y > max_scroll_y:
            # Assign without animating or repainting: this corrects bookkeeping for a
            # position the reader already occupies rather than moving them.
            self.set_reactive(Transcript.scroll_target_y, float(max_scroll_y))

    def compose(self) -> ComposeResult:
        if self._empty_wordmark is not None:
            self._empty_state = TranscriptEmptyState(
                self._empty_wordmark,
                self._empty_compact_wordmark,
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
        self._has_newer_history = False
        self._history_loading = False
        self._history_request_armed = True
        self._history_navigation = HistoryNavigation()
        self._pending_newer_navigation = HistoryNavigation()
        self._newer_navigation_scheduled = False
        self.remove_children()
        self.scroll_home(animate=False)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        # Textual updates scroll_y as the position settles (including at the end
        # of an animated user scroll) AND whenever a virtual-size change clamps a
        # parked position against a new max_scroll_y (see _size_updated) — the
        # latter carries no reader intent at all. Re-derive follow intent only
        # from a real movement: landing at the bottom means "keep following",
        # moving away means "the user is reading back, leave them there". A
        # content-driven clamp must never flip the flag to True — that would
        # mistake "the window shrank underneath a parked reader" for "the reader
        # returned to the tail" (it can land exactly on the new max_scroll_y).
        previous = self._follow
        self._scroll_without_dirtying_hidden_scrollbar(old_value, new_value)
        if not self._content_driven_scroll_update:
            if new_value < old_value:
                self._discard_pending_newer_navigation()
            self._follow = self.is_vertical_scroll_end and not self._has_newer_history
            if self._follow != previous:
                if not self._follow:
                    self._follow_generation += 1
                    # A followed Markdown stream may have armed Textual's native
                    # compositor anchor. Reader navigation owns the viewport now,
                    # so disarm it in the same state transition instead of waiting
                    # for a later stream callback or final layout.
                    self.anchor(False)
                self.post_message(self.FollowChanged(self._follow))
        if new_value > 0:
            self._history_request_armed = True
            self._history_navigation = HistoryNavigation()
        else:
            self._request_more_history_if_needed()

    def _scroll_without_dirtying_hidden_scrollbar(self, old_value: float, new_value: float) -> None:
        """Apply Textual's scroll bookkeeping without repainting an unarranged scrollbar.

        The transcript styles its scrollbar `scrollbar-size-vertical: 0`, so the bar is
        never arranged into the compositor map. Textual's own ``watch_scroll_y`` still
        assigns ``vertical_scrollbar.position``, whose reactive repaints the bar. The
        compositor then sees a dirty widget missing from the arrangement, reads it as
        newly added, and invalidates the complete map — so the next widget geometry
        lookup re-arranges *every* mounted widget. On a resumed session that is thousands
        of widgets rebuilt per scroll tick, to lay out a bar that paints nothing.

        Skip only that assignment. Anchor checking and the scroll refresh still run, so
        follow behaviour, anchoring, and painting are unchanged.
        """

        if self.show_vertical_scrollbar and not self.styles.scrollbar_size_vertical:
            if self._anchored and self._anchor_released:
                self._check_anchor()
            if round(old_value) != round(new_value):
                self._refresh_scroll()
            return
        super().watch_scroll_y(old_value, new_value)

    def history_page_loaded(self, *, has_more: bool) -> None:
        """Record one completed history page and its continuation state."""

        self._has_more_history = has_more
        self._history_loading = False
        self._history_request_armed = self._has_retained_history

    def history_page_layout_settled(self) -> None:
        """Arm edge paging after the completed page has reached stable geometry."""

        self._history_request_armed = self._has_more_history or self._has_retained_history
        if (
            self.scroll_y == 0
            and self._history_navigation.intent is not HistoryNavigationIntent.PRESERVE
        ):
            self._request_more_history_if_needed()

    def history_window_available(self, *, has_older: bool, has_newer: bool) -> None:
        """Record whether retained entries exist beyond either mounted edge."""

        self._has_retained_history = has_older
        self._has_newer_history = has_newer
        if has_older:
            self._history_request_armed = True

    @property
    def has_more_history(self) -> bool:
        """Whether a durable history page remains available."""

        return self._has_more_history

    @property
    def history_page_loading(self) -> bool:
        """Whether older-history navigation is waiting for a page response."""

        return self._history_loading

    @property
    def can_page_to_older_history(self) -> bool:
        """Whether ordinary edge paging can reveal older transcript entries."""

        return self._has_more_history or self._has_retained_history

    @property
    def can_page_to_newer_history(self) -> bool:
        """Whether forward navigation can reveal newer retained entries."""

        return self._has_newer_history

    def request_history_at_top(
        self,
        navigation: HistoryNavigation | None = None,
        *,
        rearm: bool = False,
    ) -> None:
        """Request another page only when the settled viewport remains at the top."""

        if navigation is not None:
            self._history_navigation = self._current_navigation(navigation)
        if rearm:
            self._history_request_armed = True
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
        navigation = self._current_navigation(self._history_navigation)
        self._history_navigation = HistoryNavigation()
        self.post_message(self.NeedMoreHistory(navigation))

    def _current_navigation(self, navigation: HistoryNavigation) -> HistoryNavigation:
        if navigation.reader_generation >= 0:
            return navigation
        return HistoryNavigation(
            navigation.intent,
            navigation.remaining_rows,
            self._follow_generation,
        )

    def history_page_request_failed(self) -> None:
        """Allow a transient page-load failure to be retried at the top."""

        self._history_loading = False
        self._history_request_armed = True
        self._history_navigation = HistoryNavigation()

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
        self.anchor(False)
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
        navigation: HistoryNavigation | None = None,
    ) -> None:
        """Keep the same content visible after older entries were prepended."""

        if following:
            self.return_to_latest()
            return
        navigation = navigation or HistoryNavigation()
        if navigation.intent is HistoryNavigationIntent.OLDEST:
            target_y = 0.0
        else:
            anchor_retained = anchor is not None and anchor in self.children
            navigating_forward = navigation.intent in {
                HistoryNavigationIntent.PAGE_DOWN,
                HistoryNavigationIntent.WHEEL_DOWN,
            }
            if not anchor_retained:
                # A retention boundary may replace a complete durable page, leaving
                # no widget in common. The captured ``scroll_y`` belongs to content
                # that no longer exists, so reusing it paints an unrelated offset —
                # for a backward move that means briefly showing the transcript top.
                # The adjacent edge is the only continuous fallback: the top of the
                # newer page or the bottom of the older page. Continue through that
                # edge by any rows the triggering gesture could not consume.
                direction = 1.0 if navigating_forward else -1.0
                edge_y = 0.0 if navigating_forward else self.max_scroll_y
                target_y = edge_y + direction * navigation.remaining_rows
            else:
                height_delta = anchor.virtual_region.y - anchor_y_before if anchor else 0.0
                direction = 1.0 if navigating_forward else -1.0
                target_y = scroll_y + height_delta + direction * navigation.remaining_rows
        self.restore_viewport_state(
            TranscriptViewportState(
                scroll_y=target_y,
                following=False,
            )
        )

    def _scroll_end_while_following(self) -> None:
        """Jump to the tail, re-checking follow intent when this actually runs.

        Both callers (``follow_tail()``, ``return_to_latest()``) use
        ``scroll_end(animate=False)`` with its default ``immediate=False``:
        Textual defers the real scroll via ``call_after_refresh`` so it can
        read the post-layout ``max_scroll_y``. That deferred closure applies
        unconditionally when it finally runs — it has no memory of the intent
        that scheduled it. A reader who wheels away between the call and that
        later refresh (new output arrives and schedules a follow while the
        reader is still mid-read) would otherwise be silently carried back to
        the tail regardless. Defer our own intent check the same one frame,
        then apply immediately — Textual's own deferral is already spent by
        the time ours fires, so ``max_scroll_y`` is equally settled.
        """

        def _apply_if_still_following() -> None:
            if self._follow:
                self.scroll_end(animate=False, immediate=True)

        self.call_after_refresh(_apply_if_still_following)

    def follow_tail(self) -> None:
        """Scroll to the newest content iff the user hasn't scrolled away."""
        if self._follow:
            self._scroll_end_while_following()

    def page_up(self) -> HistoryNavigation | None:
        """Move away from the tail before a page-up layout can re-pin it."""

        self._discard_pending_newer_navigation()
        self._stop_following()
        page_height = float(self.scrollable_content_region.height)
        navigation = None
        if page_height > 0 and self.scroll_y <= page_height:
            navigation = HistoryNavigation(
                HistoryNavigationIntent.PAGE_UP,
                remaining_rows=page_height - self.scroll_y,
                reader_generation=self._follow_generation,
            )
            self._history_navigation = navigation
        self.scroll_page_up(animate=False)
        self.request_history_at_top(rearm=True)
        return navigation

    def prepare_wheel_up(
        self,
        *,
        request_history: bool = True,
    ) -> HistoryNavigation | None:
        """Arm the unconsumed wheel step before Textual processes the event."""

        self._discard_pending_newer_navigation()
        self._stop_following()
        step = float(self.app.scroll_sensitivity_y)
        if self.scroll_y <= step:
            navigation = HistoryNavigation(
                HistoryNavigationIntent.WHEEL_UP,
                remaining_rows=max(0.0, step - self.scroll_y),
                reader_generation=self._follow_generation,
            )
            if request_history:
                self.request_history_at_top(navigation, rearm=True)
            return navigation
        return None

    def page_down(self) -> HistoryNavigation | None:
        """Scroll down one page and retain movement beyond the mounted edge."""

        page_height = float(self.scrollable_content_region.height)
        distance_to_end = max(0.0, self.max_scroll_y - self.scroll_y)
        navigation = None
        if self._has_newer_history and page_height > 0 and distance_to_end <= page_height:
            self._stop_following()
            navigation = HistoryNavigation(
                HistoryNavigationIntent.PAGE_DOWN,
                remaining_rows=page_height - distance_to_end,
                reader_generation=self._follow_generation,
            )
        self.scroll_page_down(animate=False)
        if navigation is not None:
            self._queue_newer_navigation(navigation)
        return navigation

    def prepare_wheel_down(self) -> HistoryNavigation | None:
        """Retain the unconsumed wheel step at the mounted newer edge."""

        step = float(self.app.scroll_sensitivity_y)
        distance_to_end = max(0.0, self.max_scroll_y - self.scroll_y)
        if distance_to_end <= 0 and not self._has_newer_history:
            return None
        # Version every effective reader move, even when it remains within the
        # mounted window. A delayed prepend restore must not overwrite a wheel
        # reversal that happened while its replacement widgets were settling.
        self._stop_following()
        if not self._has_newer_history or distance_to_end > step:
            return None
        return HistoryNavigation(
            HistoryNavigationIntent.WHEEL_DOWN,
            remaining_rows=max(0.0, step - distance_to_end),
            reader_generation=self._follow_generation,
        )

    def wheel_down(self) -> bool:
        """Apply one wheel step and request a newer window at its boundary."""

        navigation = self.prepare_wheel_down()
        moved = self._scroll_down_for_pointer(animate=False)
        if navigation is not None and self.is_vertical_scroll_end:
            self._queue_newer_navigation(navigation)
        return moved

    def _queue_newer_navigation(self, navigation: HistoryNavigation) -> None:
        """Coalesce wheel bursts before publishing one forward-edge request."""

        current = self._pending_newer_navigation
        intent = (
            navigation.intent
            if current.intent is HistoryNavigationIntent.PRESERVE
            else current.intent
        )
        self._pending_newer_navigation = HistoryNavigation(
            intent,
            current.remaining_rows + navigation.remaining_rows,
            navigation.reader_generation,
        )
        if self._newer_navigation_scheduled:
            return
        self._newer_navigation_scheduled = True
        self.call_after_refresh(self._post_pending_newer_navigation)

    def _post_pending_newer_navigation(self) -> None:
        """Publish a settled wheel burst unless later reader intent superseded it."""

        self._newer_navigation_scheduled = False
        navigation = self._pending_newer_navigation
        self._pending_newer_navigation = HistoryNavigation()
        if (
            navigation.intent is HistoryNavigationIntent.PRESERVE
            or navigation.reader_generation != self._follow_generation
            or self._follow
        ):
            return
        self.post_message(self.NeedNewerHistory(navigation))

    def _discard_pending_newer_navigation(self) -> None:
        self._pending_newer_navigation = HistoryNavigation()

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        """Preserve a wheel gesture that crosses the virtual newer edge."""

        # ``MessagePump._get_dispatch_methods`` walks the MRO and invokes *every*
        # matching handler, so ``Widget._on_mouse_scroll_down`` still runs after
        # this one and would apply a second scroll step. ``stop()`` only ends
        # bubbling to the parent; ``prevent_default()`` is what breaks that MRO
        # walk. Textual's own ``Footer`` pairs both for exactly this reason.
        if not (event.ctrl or event.shift) and self.allow_vertical_scroll and self.wheel_down():
            event.stop()
            event.prevent_default()

    def scroll_to_oldest(self) -> None:
        """Move to the durable-history boundary without waiting for scroll settlement."""

        self._discard_pending_newer_navigation()
        self._stop_following()
        self.scroll_home(animate=False)

    def stop_following(self) -> None:
        """Record explicit reader intent before a wheel scroll is processed."""

        self._stop_following()

    def _stop_following(self) -> None:
        self._follow_generation += 1
        self.anchor(False)
        if not self._follow:
            return
        self._follow = False
        self.post_message(self.FollowChanged(False))

    def return_to_latest(self) -> None:
        """Restore tail-follow intent and jump to the newest output immediately."""

        self._discard_pending_newer_navigation()
        was_following = self._follow
        self._follow_generation += 1
        self._follow = True
        self._scroll_end_while_following()
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
# colored left rail remain. Flat ToolCards also omit titles and express status in
# their action text; the remaining labels belong to operational LineMessages.
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

    def __init__(self, text: str, *, role: str) -> None:
        # Retain literal content and let theme-reactive CSS own presentation.
        # Baked Rich markup cannot be recolored atomically after a theme switch.
        super().__init__(text, markup=False)
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
    in an explicit action phrase plus a semantic role class, so the whole lifecycle
    reads as one compact tree transitioning pending → done/error instead of three
    stacked cards the reader has to reconcile. Resolved cards add a bounded result
    beneath the action using a ``└`` branch.

    Parallel calls each own a stable card regardless of finish order, because the
    registry (in ``TextualTui``) routes every event to the card for its call_id.
    """

    BINDING_GROUP_TITLE = "Tool result"
    HELP = """
    # Tool result

    Review one tool request and its bounded result. Enter or Space expands extra
    output when available; use v on an edit or write result to open its full retained
    diff. Presentation changes never rerun the tool. Escape returns focus to the
    prompt or active safety decision.
    """

    # Status drives the semantic CSS role while the visible action phrase carries
    # the same state in text, independent of theme or color support.
    _STATUS_ROLE: dict[ToolActionStatus, str] = {
        "pending": "tool",
        "denied": "denied",
        "error": "error",
        "cancelled": "denied",
        "done": "approved",
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
        Binding("v", "view_diff", "View diff", show=False),
        Binding("escape", "leave", "Back to input", show=False),
    ]

    def __init__(
        self,
        name: str,
        arguments: object,
        *,
        arguments_available: bool = True,
        track_elapsed: bool = True,
    ) -> None:
        # markup=False: every other content-bearing Static in this module
        # takes untrusted tool/model text this way. _repaint() always wraps
        # its content through Content(...) (never .from_markup), so this is
        # currently inert defense-in-depth -- but it must stay true if a
        # future call site ever updates this widget with a raw str directly.
        super().__init__("", markup=False)
        # Not `_name`: Textual's DOMNode uses `self._name` to back the widget
        # `name` property (typed str | None), so a distinct field avoids
        # shadowing it and keeps this a plain str.
        self._tool_name = name
        self._arguments_available = arguments_available
        # Retain only the compact bounded snapshot. Write/edit arguments can carry
        # complete file payloads, which settled cards must not keep alive.
        self._call_arguments = (
            format_tool_call_arguments(name, arguments) if arguments_available else Content("")
        )
        # A plain str is untrusted output escaped at repaint; a Content is an
        # already-styled renderable (e.g. a colored diff) whose text is literal,
        # so it is composed directly without markup escaping.
        self._detail: str | Content | DiffPresentation | FileResultPresentation = ""
        # The full (tool-bounded) output, kept so the reader can expand past the
        # collapsed preview/summary/diff. Untrusted text, rendered literally like a
        # str detail. Empty when there is nothing more to show than the detail.
        self._full_output: str = ""
        self._expanded = False
        # Whether the tool capped its own output; drives the honest "truncated at the
        # tool's limit" marker on the expanded view.
        self._truncated = False
        self._role = ""
        self._status: ToolActionStatus = "pending"
        # While running, `_elapsed` is a live whole-second tick count (looks alive,
        # exact precision doesn't matter mid-flight). On resolve it's replaced by
        # the true wall-clock duration derived from event timestamps (see
        # `set_state(elapsed=…)`), so the number that rests on screen is honest.
        self._elapsed: float | None = None
        self._elapsed_started_at: float | None = None
        self._clock_registered = False
        self._rendered_width: int | None = None
        self._stable_action: Content | None = None
        self._stable_action_suffix = Content()
        self._cached_body = Content()
        self._track_elapsed = track_elapsed
        self.set_state("pending")

    def on_mount(self) -> None:
        # A pending card joins the shared running counter; a card that mounts
        # already resolved (e.g. rebuilt from history) has no clock work to start.
        if self._role == "tool":
            self._start_timer(force=True)

    def _start_timer(self, *, force: bool = False) -> None:
        if not self._track_elapsed or self._clock_registered or (not force and not self.is_mounted):
            return
        if self._elapsed is None:
            self._elapsed = 0.0
        self._elapsed_started_at = time.monotonic() - self._elapsed
        _presentation_clock(self).subscribe(self, interval=self._TICK)
        self._clock_registered = True
        self._repaint()

    def on_resize(self, event: events.Resize) -> None:
        """Rewrap only when resize changes this card's effective content width."""

        width = max(8, event.size.width)
        if width != self._rendered_width:
            self._repaint(width=width)

    def update_call(self, name: str, arguments: object) -> None:
        """Enrich a historical result when its paged-in call arrives later."""

        self._tool_name = name
        self._arguments_available = True
        self._call_arguments = format_tool_call_arguments(name, arguments)
        self._repaint()

    def on_unmount(self) -> None:
        self._stop_timer()

    def _stop_timer(self) -> None:
        if self._clock_registered:
            _presentation_clock(self).unsubscribe(self)
            self._clock_registered = False
        self._elapsed_started_at = None

    def presentation_clock_tick(self, now: float) -> None:
        """Advance whole-second elapsed presentation from the shared app clock."""

        started_at = self._elapsed_started_at
        if started_at is None:
            return
        elapsed = float(int(max(0.0, now - started_at)))
        if elapsed <= (self._elapsed or 0.0):
            return
        previous = self._elapsed or 0.0
        self._elapsed = elapsed
        self._repaint(
            layout=cell_len(_format_duration(previous)) != cell_len(_format_duration(elapsed)),
            rebuild_stable=False,
        )

    def _tick(self) -> None:
        previous = self._elapsed or 0.0
        self._elapsed = previous + self._TICK
        self._repaint(
            layout=cell_len(_format_duration(previous))
            != cell_len(_format_duration(self._elapsed)),
            rebuild_stable=False,
        )

    def set_state(
        self,
        status: ToolActionStatus,
        *,
        detail: str | Content | DiffPresentation | FileResultPresentation = "",
        elapsed: float | None = None,
        full_output: str = "",
        truncated: bool = False,
    ) -> None:
        """Transition the card to a new status, action wording, and detail.

        ``detail`` adds a denial reason or bounded result preview below the persistent
        call header. A plain ``str`` is untrusted output escaped at
        repaint; a Textual ``Content`` is a pre-styled renderable (e.g. a colored
        diff) composed directly. ``elapsed`` is the true wall-clock duration (from
        the request/result event timestamps); passing it freezes the live counter
        at the honest value and leaves the shared presentation clock. ``full_output`` is the
        tool's full (tool-bounded) output, retained so the reader can expand past the
        collapsed detail; ``truncated`` says the tool itself capped that output. The
        role CSS class is swapped rather than added so the rail reflects only the
        current state.
        """

        role = self._STATUS_ROLE[status]
        self._status = status
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
        else:
            # A process lifecycle may recover from a denied or failed observation
            # when a later poll arrives on the same stable card.
            self._start_timer()
        if role != self._role:
            if self._role:
                # Keep the prior role out of the next style resolution without
                # briefly exposing the generic ``message`` geometry in between.
                self.remove_class(f"message--{self._role}", update=False)
            self.add_class("message", f"message--{role}")
            self._role = role
        self.border_title = ""
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
        if isinstance(self._detail, FileResultPresentation):
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
        # The content update below invalidates layout immediately. Disarm an
        # active Markdown stream anchor before that invalidation can be composed;
        # the app will explicitly re-pin a newest card when focus began at the
        # tail, while an older card must keep its own top in view.
        transcript = next(
            (ancestor for ancestor in self.ancestors if isinstance(ancestor, Transcript)),
            None,
        )
        if transcript is not None:
            transcript.anchor(False)
        self._expanded = not self._expanded
        self._repaint()
        # A followed transcript should stay pinned to the tail when the *newest* card
        # grows; the app decides using the follow intent captured at focus and this
        # card's position (expanding a historical card must not yank the viewport).
        self.post_message(self.Toggled(self))

    def action_view_diff(self) -> None:
        """Open the retained structured diff in the dedicated reader surface."""

        if isinstance(self._detail, DiffPresentation):
            self.post_message(self.ViewDiffRequested(self, self._detail))

    def _expanded_supplement(self, *, width: int) -> Content:
        """Return subclass-owned bounded content shown only while expanded."""

        del width
        return Content()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "toggle_expand":
            return self._can_expand()
        if action == "view_diff":
            return isinstance(self._detail, DiffPresentation)
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

    class ViewDiffRequested(Message):
        """A focused diff card requested the dedicated reader surface."""

        def __init__(self, card: ToolCard, presentation: DiffPresentation) -> None:
            super().__init__()
            self.card = card
            self.presentation = presentation

    def _repaint(
        self,
        *,
        layout: bool = True,
        rebuild_stable: bool = True,
        width: int | None = None,
    ) -> None:
        """Paint the card, retaining width-dependent detail across elapsed ticks."""

        selected_width = max(
            8,
            width if width is not None else self.content_size.width or self.size.width or 80,
        )
        width_changed = selected_width != self._rendered_width
        if rebuild_stable or self._stable_action is None:
            self._stable_action = self._action_content()
            suffix = Content()
            # Label the affordance so a reader does not have to infer what a bare
            # triangle means. Enter is the primary binding; Space remains supported.
            if self._can_expand():
                if not isinstance(self._detail, FileResultPresentation):
                    label = " ▾ less (Enter)" if self._expanded else " ▸ more (Enter)"
                    suffix += Content.styled(label, "$text-muted")
            if isinstance(self._detail, DiffPresentation):
                suffix += Content.styled(" · v view diff", "$text-muted")
            self._stable_action_suffix = suffix
        if rebuild_stable or width_changed:
            self._cached_body = self._build_body(width=selected_width)

        assert self._stable_action is not None
        elapsed = (
            Content.styled(f" · {_format_duration(self._elapsed)}", "$text-muted")
            if self._elapsed is not None
            else Content()
        )
        action = self._stable_action + elapsed + self._stable_action_suffix
        content = _tree_line(
            action,
            width=selected_width,
            first_prefix="• ",
            continuation_prefix="  ",
        )
        content += self._cached_body
        self._rendered_width = selected_width
        self.update(content, layout=layout)

    def _build_body(self, *, width: int) -> Content:
        """Build detail, diff, and supplement content only on structural changes."""

        body = Content()
        if isinstance(self._detail, DiffPresentation):
            # Structured edit/write cards retain diff rows for both states; unlike
            # generic tools, expansion must never replace review evidence with the
            # raw "Applied" or "Wrote" acknowledgement kept in _full_output.
            body += Content("\n") + _render_diff_presentation(
                self._detail,
                width=max(12, width),
                expanded=self._expanded,
            )
        elif isinstance(self._detail, FileResultPresentation):
            body += Content("\n") + _tree_detail(
                _file_result_detail(
                    self._detail,
                    width=width,
                    expanded=self._expanded,
                ),
                width=width,
            )
        elif self._expanded and self._full_output:
            # Expanded: show the full (tool-bounded) output in place of the collapsed
            # detail, so the reader sees what the preview/summary stood in for.
            body += Content("\n") + _tree_detail(self._full_output, width=width)
        elif isinstance(self._detail, Content):
            # A pre-styled renderable is composed directly, preserving literal text.
            body += Content("\n") + _tree_detail(self._detail, width=width)
        elif self._detail:
            body += Content("\n") + _tree_detail(self._detail, width=width)

        supplement = self._expanded_supplement(width=width)
        if supplement:
            body += Content("\n") + supplement

        if self._truncated:
            # The tool capped its own output before it ever reached here, so what the
            # card shows isn't complete even when no additional output can expand.
            body += Content("\n") + Content.styled(
                "    ⋯ output truncated at the tool's limit", "$warning"
            )
        return body

    def _action_content(self) -> Content:
        return _format_tool_call_action_from_rendered(
            self._tool_name,
            self._call_arguments,
            status=self._status,
            arguments_available=self._arguments_available,
        )


def _process_id_for_display(process_id: str) -> str:
    """Bound and flatten an untrusted process ID before building Textual content."""

    # Slice before normalization so even a malformed multi-megabyte argument has
    # constant presentation cost. Managed IDs are 32 lowercase hexadecimal chars.
    single_line = " ".join(process_id[:64].split()) or "(invalid process id)"
    return _truncate_to_cell_width(single_line, _PROCESS_ID_DISPLAY_CELLS)


class ProcessCard(ToolCard):
    """One bounded presentation card spanning repeated polls for a process ID."""

    HELP = """
    # Process history

    Enter or Space expands the represented process timeline. Use p and n to select
    an earlier or later persisted update, then l to load that row's exact persisted
    output. Loading history never reruns the process. Tool-level truncation markers
    mean the discarded bytes were not persisted and cannot be recovered.
    """

    _STATE_WORDS = {
        "polling": "Polling process",
        "cancelling": "Cancelling process",
        "running": "Running process",
        "completed": "Process completed",
        "failed": "Process failed",
        "timed_out": "Process timed out",
        "cancelled": "Process cancelled",
        "poll_denied": "Process poll denied",
        "cancel_denied": "Process cancellation denied",
        "poll_interrupted": "Process poll interrupted",
        "cancel_interrupted": "Process cancellation interrupted",
        "poll_failed": "Process poll failed",
        "cancel_failed": "Process cancellation failed",
        "observed": "Observed process",
    }
    _STATUS_STYLE = {
        "pending": "bold $accent",
        "done": "bold $success",
        "error": "bold $error",
        "denied": "bold $warning",
        "cancelled": "bold $warning",
    }
    _HISTORY_UPDATE_WINDOW = 6
    BINDINGS = [
        *ToolCard.BINDINGS,
        Binding("p", "previous_history_update", "Previous process update", show=False),
        Binding("n", "next_history_update", "Next process update", show=False),
        Binding("l", "load_history_output", "Load persisted output", show=False),
    ]

    def __init__(self, process_id: str, *, track_elapsed: bool = True) -> None:
        self._process_id = process_id
        self._process_presentation = ProcessLifecyclePresentation(
            process_id=process_id,
            display_state="observed",
            poll_count=0,
            call_count=0,
            detail="(no process output yet)",
            full_output="",
            retained_output="",
            source_truncated=False,
            ui_dropped_bytes=0,
        )
        self._history_update_index = 0
        self._history_detail_entry_id: str | None = None
        self._history_detail_output = ""
        self._history_detail_loading = False
        self._history_detail_error = ""
        super().__init__(
            "bash",
            {"operation": "poll", "process_id": process_id},
            track_elapsed=track_elapsed,
        )

    @property
    def process_id(self) -> str:
        return self._process_id

    @property
    def lifecycle_presentation(self) -> ProcessLifecyclePresentation:
        """Return the latest bounded snapshot for history-to-live transfer."""

        return self._process_presentation

    def start_live_updates(self) -> None:
        """Enable elapsed tracking when a resumed historical card becomes live."""

        self._track_elapsed = True
        self._start_timer()

    def set_lifecycle(
        self,
        presentation: ProcessLifecyclePresentation,
        *,
        elapsed: float | None = None,
    ) -> None:
        """Replace the bounded lifecycle snapshot and repaint this card in place."""

        had_history_updates = bool(self._process_presentation.history_updates)
        self._process_presentation = presentation
        self._detail = presentation.detail
        self._full_output = "" if presentation.history_updates else presentation.full_output
        self._truncated = presentation.source_truncated
        if presentation.history_updates:
            self._history_update_index = (
                min(self._history_update_index, len(presentation.history_updates) - 1)
                if had_history_updates
                else len(presentation.history_updates) - 1
            )
        state = presentation.display_state
        if state == "completed":
            status: ToolActionStatus = "done"
        elif state in {"failed", "timed_out", "poll_failed", "cancel_failed"}:
            status = "error"
        elif state in {"cancelled", "poll_interrupted", "cancel_interrupted"}:
            status = "cancelled"
        elif state in {"poll_denied", "cancel_denied"}:
            status = "denied"
        else:
            status = "pending"
        self.set_state(
            status,
            elapsed=elapsed,
            truncated=presentation.source_truncated,
        )

    def _action_content(self) -> Content:
        presentation = self._process_presentation
        words = self._STATE_WORDS[presentation.display_state]
        content = Content.styled(words, self._STATUS_STYLE[self._status])
        content += Content(" ") + Content.styled(
            _process_id_for_display(self._process_id),
            "$secondary",
        )
        if presentation.poll_count:
            unit = "poll" if presentation.poll_count == 1 else "polls"
            content += Content.styled(
                f" · {presentation.poll_count} {unit}",
                "$text-muted",
            )
        if presentation.history_entry_ids:
            row_count = len(presentation.history_entry_ids)
            content += Content.styled(
                f" · {row_count}/{row_count} rows",
                "$text-muted",
            )
        return content

    def _can_expand(self) -> bool:
        return bool(self._process_presentation.history_updates) or super()._can_expand()

    def action_previous_history_update(self) -> None:
        updates = self._process_presentation.history_updates
        if not self._expanded or not updates:
            return
        self._history_update_index = max(0, self._history_update_index - 1)
        self._clear_history_detail_if_selection_changed()
        self._repaint()

    def action_next_history_update(self) -> None:
        updates = self._process_presentation.history_updates
        if not self._expanded or not updates:
            return
        self._history_update_index = min(len(updates) - 1, self._history_update_index + 1)
        self._clear_history_detail_if_selection_changed()
        self._repaint()

    def action_load_history_output(self) -> None:
        updates = self._process_presentation.history_updates
        if not self._expanded or not updates or self._history_detail_loading:
            return
        selected = updates[self._history_update_index]
        if self._history_detail_entry_id == selected.entry_id and self._history_detail_output:
            return
        self._history_detail_loading = True
        self._history_detail_error = ""
        self._repaint()
        self.post_message(self.HistoryDetailRequested(self, selected.entry_id))

    def history_detail_loaded(self, entry_id: str, output: str) -> None:
        updates = self._process_presentation.history_updates
        if not updates or updates[self._history_update_index].entry_id != entry_id:
            return
        self._history_detail_entry_id = entry_id
        self._history_detail_output = output
        self._history_detail_loading = False
        self._history_detail_error = ""
        self._repaint()

    def history_detail_failed(self, entry_id: str, error: str) -> None:
        updates = self._process_presentation.history_updates
        if not updates or updates[self._history_update_index].entry_id != entry_id:
            return
        self._history_detail_loading = False
        self._history_detail_error = error
        self._repaint()

    def _clear_history_detail_if_selection_changed(self) -> None:
        updates = self._process_presentation.history_updates
        if (
            updates
            and self._history_detail_entry_id != updates[self._history_update_index].entry_id
        ):
            self._history_detail_output = ""
            self._history_detail_error = ""
            self._history_detail_loading = False

    def _expanded_supplement(self, *, width: int) -> Content:
        updates = self._process_presentation.history_updates
        if not self._expanded or not updates:
            return Content()
        selected_index = min(self._history_update_index, len(updates) - 1)
        half = self._HISTORY_UPDATE_WINDOW // 2
        start = min(
            max(0, selected_index - half),
            max(0, len(updates) - self._HISTORY_UPDATE_WINDOW),
        )
        stop = min(len(updates), start + self._HISTORY_UPDATE_WINDOW)
        lines = [f"    updates {start + 1}–{stop} of {len(updates)} · p/n navigate · l load output"]
        for index in range(start, stop):
            update = updates[index]
            marker = "▸" if index == selected_index else " "
            state = update.display_state.replace("_", " ")
            lines.append(f"    {marker} #{index + 1} {state}: {update.preview}")
        if self._history_detail_loading:
            lines.append("    ⟳ Loading persisted output…")
        elif self._history_detail_error:
            lines.append(f"    ! {self._history_detail_error} · l retry")
        elif self._history_detail_output:
            detail_lines = self._history_detail_output.splitlines() or ["(empty persisted output)"]
            lines.append(f"    persisted output · {len(detail_lines)} lines")
            lines.extend(f"      {line}" for line in detail_lines)
        return _tree_detail("\n".join(lines), width=width)

    class HistoryDetailRequested(Message):
        """The reader requested exact persisted output for one represented update."""

        def __init__(self, card: ProcessCard, entry_id: str) -> None:
            super().__init__()
            self.card = card
            self.entry_id = entry_id


_ACTIVITY_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_ACTIVITY_SPINNER_INTERVAL = 0.08


class WorkingIndicator(Static):
    """Command-scoped heartbeat shown in the *transcript*, not the footer.

    A dim ``⠋ Working… · 3s`` row remains at the live transcript tail while the
    command has not produced visible assistant output. It may be relabeled for
    retries, approvals, trust, or compaction. The first visible stream frame retires
    it to avoid a completion-time layout jump; tool-only commands retain it until
    settlement. The footer stays stable (cwd / session / model) — quiet over noisy.
    """

    _FRAMES = _ACTIVITY_SPINNER_FRAMES
    _INTERVAL = _ACTIVITY_SPINNER_INTERVAL  # ~12.5 fps braille rotation

    def __init__(self) -> None:
        super().__init__("", markup=False)
        self.add_class("message", "message--dim")
        self._ticks = 0
        self._label = "Working…"
        self._show_elapsed = True
        self._animation_mounted = False
        self._clock_registered = False
        self._rendered_width: int | None = None

    def on_mount(self) -> None:
        self._animation_mounted = True
        self._start_timer()
        self._repaint()

    def on_unmount(self) -> None:
        self._animation_mounted = False
        self._stop_timer()

    def presentation_clock_tick(self, now: float) -> None:
        """Advance the heartbeat from the app-owned clock."""

        del now
        self._tick()

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
        if not self._clock_registered:
            self._start_timer()
        self._label = "Working…"
        self._show_elapsed = True
        self._repaint()

    def show_activity(self, label: str, *, show_elapsed: bool = True) -> None:
        """Relabel this command heartbeat without resetting its elapsed time."""

        if not self._clock_registered:
            self._start_timer()
        self._label = label
        self._show_elapsed = show_elapsed
        self._repaint()

    def _start_timer(self) -> None:
        if not self._clock_registered and self._animation_mounted:
            _presentation_clock(self).subscribe(self, interval=self._INTERVAL)
            self._clock_registered = True

    def _stop_timer(self) -> None:
        if self._clock_registered:
            _presentation_clock(self).unsubscribe(self)
            self._clock_registered = False

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


class StartupNotice(Static):
    """Startup state shown immediately above the composer surface."""

    _FRAMES = _ACTIVITY_SPINNER_FRAMES
    _INTERVAL = _ACTIVITY_SPINNER_INTERVAL

    def __init__(self, *, input_ready: bool = True, id: str | None = None) -> None:  # noqa: A002
        super().__init__("", id=id, markup=False)
        self._starting = not input_ready
        self._draft_preserved = False
        self._animation_suspended = False
        self._animation_mounted = False
        self._ticks = 0
        self._clock_registered = False
        self._rendered_width: int | None = None
        self.display = self._starting

    def on_mount(self) -> None:
        # Textual's public ``is_mounted`` flag is still false while on_mount is
        # dispatched. Track this widget's animation lifecycle explicitly so a
        # notice constructed in the starting state can register its cold-start
        # clock instead of waiting for a state transition that may never occur.
        self._animation_mounted = True
        self._start_timer()
        self._repaint()

    def on_unmount(self) -> None:
        self._animation_mounted = False
        self._stop_timer()

    def set_input_ready(self, ready: bool) -> None:
        starting = not ready
        if starting == self._starting:
            return
        self._starting = starting
        self.display = starting
        if starting:
            self._draft_preserved = False
            self._ticks = 0
            self._start_timer()
            self._repaint()
        else:
            self._stop_timer()

    def show_draft_preserved(self) -> None:
        """Confirm that an early Enter left the current editor draft intact."""

        if not self._starting:
            return
        self._draft_preserved = True
        self._repaint()

    def suspend_animation(self) -> None:
        """Pause animation while an overlay hides the composer."""

        self._animation_suspended = True
        self._stop_timer()

    def resume_animation(self) -> None:
        """Resume animation after the composer becomes visible again."""

        self._animation_suspended = False
        self._start_timer()
        self._repaint()

    def presentation_clock_tick(self, now: float) -> None:
        """Advance startup animation from the app-owned clock."""

        del now
        self._tick()

    def _tick(self) -> None:
        self._ticks += 1
        self._repaint()

    def _start_timer(self) -> None:
        if (
            not self._clock_registered
            and self._starting
            and not self._animation_suspended
            and self._animation_mounted
        ):
            _presentation_clock(self).subscribe(self, interval=self._INTERVAL)
            self._clock_registered = True

    def _stop_timer(self) -> None:
        if self._clock_registered:
            _presentation_clock(self).unsubscribe(self)
            self._clock_registered = False

    def _repaint(self) -> None:
        spinner = self._FRAMES[self._ticks % len(self._FRAMES)]
        label = (
            "Wisp is still starting — your draft is preserved."
            if self._draft_preserved
            else "Starting Wisp… You can start typing while it gets ready."
        )
        text = f"{spinner} {label}"
        width = len(text)
        self.update(text, layout=width != self._rendered_width)
        self._rendered_width = width


@dataclass(frozen=True)
class _TextualFooterParts:
    """Semantic fields for Textual's single-row adaptive footer."""

    left: str
    activity: str
    center: str
    billing_route: str
    session_cost: str
    context_wide: str
    context_compact: str

    @property
    def billing(self) -> str:
        return _joined_footer_fields(self.billing_route, self.session_cost)


def _textual_footer_parts(snapshot: TuiViewSnapshot) -> _TextualFooterParts:
    queue_parts = []
    if snapshot.queued_steering:
        queue_parts.append(f"{snapshot.queued_steering} steer")
    if snapshot.queued_follow_ups:
        queue_parts.append(f"{snapshot.queued_follow_ups} later")
    activity = " · ".join(queue_parts)
    cwd = _sanitize_footer_text(_format_cwd_for_footer(snapshot.cwd))
    left = " · ".join(part for part in (cwd, activity) if part)
    center = (
        ""
        if not snapshot.input_ready
        else "↵ steer · alt+↵ later · esc cancel"
        if snapshot.input_mode == "running"
        else "↵ send · / commands"
        if snapshot.input_mode == "idle"
        else ""
    )
    context_wide, context_compact = _textual_context_parts(snapshot)
    billing_route, session_cost = _textual_billing_parts(snapshot)
    return _TextualFooterParts(
        left=left,
        activity=activity,
        center=center,
        billing_route=billing_route,
        session_cost=session_cost,
        context_wide=context_wide,
        context_compact=context_compact,
    )


def _textual_billing_parts(snapshot: TuiViewSnapshot) -> tuple[str, str]:
    if snapshot.provider == "openai-codex":
        route = "ChatGPT plan"
    elif snapshot.provider == "fake":
        route = "offline"
    elif snapshot.provider is None:
        route = ""
    else:
        route = "API"

    cost = snapshot.cost
    if cost is None or (cost.priced_record_count == 0 and cost.unpriced_record_count == 0):
        return route, ""
    summary = format_cost_summary(cost)
    session_cost = (
        "session unpriced"
        if summary == "cost unknown"
        else f"session {summary.removeprefix('cost ')}"
    )
    return route, session_cost


def _textual_context_parts(snapshot: TuiViewSnapshot) -> tuple[str, str]:
    context = snapshot.context
    if context is None:
        return "", ""
    observed_is_current = (
        context.observed_is_current
        and context.observed_tokens is not None
        and context.context_window is not None
    )
    effective_tokens = (
        context.effective_tokens
        if context.accounting_method == "provider_observed_plus_estimate"
        and context.effective_tokens is not None
        else context.observed_tokens
    )
    percent = context.estimated_percent
    if observed_is_current and effective_tokens is not None:
        assert context.context_window is not None
        percent = effective_tokens / context.context_window * 100
    if percent is not None:
        marker = "" if observed_is_current else "~"
        compact = f"{marker}{percent:.0f}%"
        return f"context {compact}", compact

    compact = _footer_context_text(context)
    if not compact:
        return "", ""
    return compact.replace("ctx ", "context ", 1), compact


def _joined_footer_fields(*parts: str) -> str:
    return " · ".join(part for part in parts if part)


def _position_footer_fields(
    left: str,
    center: str,
    right: str,
    width: int | None,
) -> str | None:
    """Place three fields without overlap, returning ``None`` when they collide."""

    if width is None:
        return "  ".join(part for part in (left, center, right) if part)
    left_width = cell_len(left)
    center_width = cell_len(center)
    right_width = cell_len(right)
    if max(left_width, center_width, right_width) > width:
        return None

    right_start = width - right_width
    if not center:
        if left and right and left_width + 2 > right_start:
            return None
        return left + " " * max(0, right_start - left_width) + right

    center_start = (width - center_width) // 2
    if left and left_width + 2 > center_start:
        return None
    if right and center_start + center_width + 2 > right_start:
        return None
    return (
        left
        + " " * max(0, center_start - left_width)
        + center
        + " " * max(0, right_start - center_start - center_width)
        + right
    )


def _format_textual_footer_line(
    parts: _TextualFooterParts,
    *,
    width: int | None,
) -> str:
    full_right = _joined_footer_fields(parts.billing, parts.context_wide)
    compact_context = _joined_footer_fields(parts.billing, parts.context_compact)
    candidates = [
        (parts.left, parts.center, full_right),
        (parts.left, "esc cancel" if "esc cancel" in parts.center else "", full_right),
        (parts.left, "", full_right),
        (parts.left, "", compact_context),
    ]
    if parts.activity and parts.activity != parts.left:
        candidates.extend(
            (parts.activity, "", right)
            for right in (
                compact_context,
                parts.billing,
                parts.billing_route,
                parts.session_cost,
                parts.context_compact,
            )
            if right
        )
    candidates.extend(
        ("", "", right)
        for right in (
            compact_context,
            parts.billing,
            parts.billing_route,
            parts.session_cost,
            parts.context_compact,
        )
        if right
    )
    candidates.extend((left, "", "") for left in (parts.activity, parts.left) if left)

    selected_width = max(1, width) if width is not None else None
    for left, center, right in dict.fromkeys(candidates):
        line = _position_footer_fields(left, center, right, selected_width)
        if line is not None:
            return line

    fallback = (
        parts.activity
        or parts.billing_route
        or parts.session_cost
        or parts.context_compact
        or parts.left
    )
    return _truncate_to_cell_width(fallback, selected_width)


def _composer_metadata_fields(
    snapshot: TuiViewSnapshot,
    *,
    width: int | None,
) -> tuple[str, str, str]:
    """Return mode, model, and provider fields that fit the composer meta row."""

    mode = "Plan" if snapshot.mode == "plan" else "Build"
    model = _sanitize_footer_text(snapshot.model or "")
    provider = _sanitize_footer_text(snapshot.provider or "")
    fields = (mode, model, provider)
    if width is None:
        return fields

    selected_width = max(1, width)
    if cell_len(_joined_footer_fields(*fields)) <= selected_width:
        return fields
    if cell_len(_joined_footer_fields(mode, model)) <= selected_width:
        return mode, model, ""
    if model:
        available_model_width = selected_width - cell_len(mode) - cell_len(" · ")
        if available_model_width > 0:
            return mode, _truncate_to_cell_width(model, available_model_width), ""
    return _truncate_to_cell_width(mode, selected_width), "", ""


class ComposerMeta(Static):
    """Mode and model metadata displayed inside the composer surface."""

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 - Textual API
        super().__init__(id=id, markup=False)
        self._snapshot = TuiViewSnapshot(status="idle", input_hint="wisp> ")
        self._compact = False

    def on_mount(self) -> None:
        self._render_metadata()

    def on_resize(self, event: events.Resize) -> None:
        self._render_metadata()

    def set_snapshot(self, snapshot: TuiViewSnapshot) -> None:
        self._snapshot = snapshot
        self._render_metadata()

    def set_compact(self, compact: bool) -> None:
        """Show only the behavior-changing mode when vertical space is scarce."""

        if self._compact == compact:
            return
        self._compact = compact
        self._render_metadata()

    def refresh_theme(self) -> None:
        """Re-resolve the mode accent after a live theme change."""

        self._render_metadata()

    def _render_metadata(self) -> None:
        mode, model, provider = _composer_metadata_fields(
            self._snapshot,
            width=(self.content_size.width or None),
        )
        if self._compact:
            model = ""
            provider = ""
        rendered = Text()
        theme = self.app.current_theme
        for value, color in (
            (mode, theme.accent),
            (model, theme.foreground),
            (provider, None),
        ):
            if not value:
                continue
            if rendered:
                rendered.append(" · ")
            start = len(rendered)
            rendered.append(value)
            if color is not None:
                rendered.stylize(color, start, len(rendered))
        self.update(rendered)


class PendingInputPreview(Static):
    """Bounded composer-attached view of accepted prompts waiting to start."""

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id, markup=False)
        self._submissions: tuple[PendingSubmissionView, ...] = ()
        self.display = False

    def on_resize(self, event: events.Resize) -> None:
        self._render_preview()

    def set_submissions(self, submissions: tuple[PendingSubmissionView, ...]) -> None:
        if submissions == self._submissions:
            return
        self._submissions = submissions
        self.display = bool(submissions)
        self._render_preview()

    def _render_preview(self) -> None:
        width = self.content_size.width or self.size.width or None
        self.update("\n".join(pending_submission_preview_lines(self._submissions, width=width)))


class ComposerPanel(Vertical):
    """Focusable composer body that groups the editor and its metadata."""

    def __init__(
        self,
        *,
        placeholder: str = "",
        id: str | None = None,  # noqa: A002 - Textual's parameter name
    ) -> None:
        super().__init__(id=id)
        self._pending = PendingInputPreview(id="pending-input")
        self._input = PromptEditor(placeholder=placeholder, id="input")
        self._metadata = ComposerMeta(id="composer-meta")

    def compose(self) -> ComposeResult:
        yield self._pending
        yield self._input
        yield self._metadata

    def on_mount(self) -> None:
        self.refresh_layout()

    def set_snapshot(self, snapshot: TuiViewSnapshot) -> None:
        self._pending.set_submissions(snapshot.pending_submissions)
        self._metadata.set_snapshot(snapshot)

    def refresh_theme(self) -> None:
        self._metadata.refresh_theme()

    def refresh_layout(self, *, height: int | None = None) -> None:
        """Recompute responsive content from the terminal height."""

        selected_height = self.app.size.height if height is None else height
        compact = selected_height < 16
        self.set_class(compact, "-compact")
        self._metadata.set_compact(compact)
        self._input.styles.max_height = max(6, selected_height // 3)

    def hide(self) -> None:
        """Hide the writing surface and mark its editor unavailable to input routing."""

        self._input.display = False
        self.display = False

    def show(self) -> None:
        """Restore the writing surface and its editor after an overlay closes."""

        self.display = True
        self._input.display = True

    def focus(self, scroll_visible: bool = True) -> ComposerPanel:
        self._input.focus(scroll_visible=scroll_visible)
        return self

    def on_click(self, event: events.Click) -> None:
        """Make the panel's rail and padding a focus affordance, not dead space."""

        if event.control is self:
            self.focus()


class ComposerRegion(Vertical):
    """Composer slot containing external status chrome and the writing surface."""

    def __init__(
        self,
        *,
        placeholder: str = "",
        input_ready: bool = True,
        id: str | None = None,  # noqa: A002 - Textual's parameter name
    ) -> None:
        super().__init__(id=id)
        self._startup = StartupNotice(input_ready=input_ready, id="startup-notice")
        self._composer = ComposerPanel(placeholder=placeholder, id="composer")

    def compose(self) -> ComposeResult:
        yield self._startup
        yield self._composer

    def set_snapshot(self, snapshot: TuiViewSnapshot) -> None:
        self._startup.set_input_ready(snapshot.input_ready)
        self._composer.set_snapshot(snapshot)

    def show_startup_submission_blocked(self) -> None:
        self._startup.show_draft_preserved()

    def refresh_theme(self) -> None:
        self._composer.refresh_theme()

    def refresh_layout(self, *, height: int | None = None) -> None:
        self._composer.refresh_layout(height=height)

    def hide(self) -> None:
        """Hide the complete input slot while an overlay owns its space."""

        self._startup.suspend_animation()
        self._composer.hide()
        self.display = False

    def show(self) -> None:
        """Restore the complete input slot after its overlay closes."""

        self.display = True
        self._composer.show()
        self._startup.resume_animation()

    def focus(self, scroll_visible: bool = True) -> ComposerRegion:
        self._composer.focus(scroll_visible=scroll_visible)
        return self


class StatusBar(Static):
    """Textual-only single-row footer with adaptive context and billing fields."""

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
        parts = _textual_footer_parts(self._snapshot)
        line = _format_textual_footer_line(parts, width=width if width > 0 else None)
        rendered = Text(line)
        if parts.billing and (start := line.rfind(parts.billing)) >= 0:
            theme = self.app.current_theme
            accent = theme.accent or theme.foreground
            if accent is not None:
                rendered.stylize(accent, start, start + len(parts.billing))
        self.update(rendered)

    def refresh_theme(self) -> None:
        """Re-resolve the billing accent after a live theme change."""

        self._render_status()


type _CodeBlockRenderCache = dict[int, tuple[int, tuple[Segment, ...]]]


@dataclass(frozen=True, slots=True)
class _MarkdownRenderConfig:
    theme: RichTheme
    code_theme: str
    native_ansi: bool


class _AssistantCodeBlock(CodeBlock):
    """Compact, wrapping code fence for the single-widget assistant renderer."""

    @classmethod
    def create(cls, markdown: RichMarkdown, token: object) -> _AssistantCodeBlock:
        # markdown-it's Token is intentionally kept at this parser boundary. Rich's
        # public CodeBlock factory reads only ``info``; mirror that without importing
        # markdown-it as another direct dependency.
        info = str(getattr(token, "info", "") or "")
        lexer_name = info.partition(" ")[0] or "text"
        cacheable_token_ids: frozenset[int] = getattr(
            markdown, "cacheable_fence_token_ids", frozenset()
        )
        return cls(
            lexer_name,
            markdown.code_theme,
            native_ansi=bool(getattr(markdown, "native_ansi", False)),
            cache_key=id(token) if id(token) in cacheable_token_ids else None,
            render_cache=getattr(markdown, "code_block_render_cache", None),
        )

    def __init__(
        self,
        lexer_name: str,
        theme: str,
        *,
        native_ansi: bool,
        cache_key: int | None,
        render_cache: _CodeBlockRenderCache | None,
    ) -> None:
        super().__init__(lexer_name, theme)
        self._native_ansi = native_ansi
        self._cache_key = cache_key
        self._render_cache = render_cache

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        cache_key = self._cache_key
        render_cache = self._render_cache
        if cache_key is not None and render_cache is not None:
            cached = render_cache.get(cache_key)
            if cached is not None and cached[0] == options.max_width:
                yield from cached[1]
                return

        code = str(self.text).rstrip()
        renderable: RenderableType
        if self.lexer_name.lower() == "ansi":
            decoded = Text.from_ansi(code)
            renderable = decoded if self._native_ansi else Text(decoded.plain)
        else:
            renderable = Syntax(
                code,
                self.lexer_name,
                theme=self.theme,
                word_wrap=True,
                background_color="default",
                padding=(0, 2),
            )
        rendered = tuple(console.render(renderable, options))
        if cache_key is not None and render_cache is not None:
            # Retain one width per closed fence. Resizes replace, rather than
            # multiply, width-dependent highlighted segment trees.
            render_cache[cache_key] = (options.max_width, rendered)
        yield from rendered


class _AssistantHeading(Heading):
    """Keep every heading left-aligned in a terminal conversation."""

    LEVEL_ALIGN: ClassVar = {f"h{level}": "left" for level in range(1, 7)}


class _AssistantMarkdown(RichMarkdown):
    """Rich Markdown with Wisp theming and Textual-safe clickable links."""

    elements: ClassVar = {
        **RichMarkdown.elements,
        "heading_open": _AssistantHeading,
        "fence": _AssistantCodeBlock,
        "code_block": _AssistantCodeBlock,
    }

    def __init__(
        self,
        source: str,
        *,
        theme: RichTheme,
        code_theme: str,
        native_ansi: bool,
    ) -> None:
        super().__init__(source, code_theme=code_theme, hyperlinks=True)
        self._wisp_theme = theme
        self.native_ansi = native_ansi
        self.cacheable_fence_token_ids: frozenset[int] = frozenset()
        self.code_block_render_cache: _CodeBlockRenderCache | None = None

    def enable_stable_fence_cache(
        self,
        token_ids: frozenset[int],
        render_cache: _CodeBlockRenderCache,
    ) -> None:
        """Reuse highlighted output only for immutable, closed fence tokens."""

        self.cacheable_fence_token_ids = token_ids
        self.code_block_render_cache = render_cache

    def release_render_cache(self) -> None:
        """Stop retaining streaming-only highlighted segments after settlement."""

        self.cacheable_fence_token_ids = frozenset()
        self.code_block_render_cache = None

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        with console.use_theme(self._wisp_theme):
            for renderable in super().__rich_console__(console, options):
                segments = (
                    (renderable,)
                    if isinstance(renderable, Segment)
                    else console.render(renderable, options)
                )
                for segment in segments:
                    style = segment.style
                    if style is not None and style.link:
                        action = f"open_markdown_link({style.link!r})"
                        style = style.update_link(None) + RichStyle(meta={"@click": action})
                    yield Segment(segment.text, style, segment.control)


class _SafeAssistantMarkdown:
    """Render a prepared Markdown document, preserving source on render failure."""

    def __init__(self, source: str, markdown: _AssistantMarkdown) -> None:
        self.source = source
        self.markdown = markdown

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        try:
            yield from console.render(self.markdown, options)
        except Exception:
            yield Text(self.source)


class _SelectableMarkdownVisual(RichVisual):
    """Rich Markdown visual with rendered-row offsets and selection styling."""

    def __init__(self, widget: Widget, renderable: RenderableType) -> None:
        super().__init__(widget, renderable)
        self._markdown_renderable = renderable
        self._base_strips_cache: tuple[int, Style, tuple[Strip, ...]] | None = None
        self.plain = ""

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        # Preserve StreamMessage.content's existing Rich-renderable contract for
        # focused presentation tests and callers outside Textual's Visual path.
        yield from console.render(self._markdown_renderable, options)

    def _apply_selection_style(
        self,
        strip: Strip,
        base_style: Style,
        selection_style: Style,
    ) -> Strip:
        """Blend Textual's selection overlay on top of each Rich segment."""

        ansi_theme = self._widget.app.ansi_theme
        segments: list[Segment] = []
        for segment in strip:
            if segment.control:
                segments.append(segment)
                continue
            rich_style = segment.style or base_style.rich_style
            resolved_style = (
                Style.from_rich_style(rich_style, ansi_theme) + selection_style
            ).rich_style
            segments.append(Segment(segment.text, resolved_style, segment.control))
        return Strip(segments, strip.cell_length)

    def _base_strips(
        self,
        width: int,
        style: Style,
        options: RenderOptions,
    ) -> tuple[Strip, ...]:
        cached = self._base_strips_cache
        if cached is not None and cached[:2] == (width, style):
            return cached[2]
        strips = tuple(super().render_strips(width, None, style, options))
        self._base_strips_cache = (width, style, strips)
        return strips

    def get_height(self, rules: RulesMap, width: int) -> int:
        """Measure through the same immutable strips used by the following paint."""

        options = RenderOptions(self._widget._get_style, rules)
        return len(self._base_strips(width, self._widget.visual_style, options))

    @property
    def has_measured_empty_render(self) -> bool:
        """Whether layout measured this Markdown document as zero visible rows."""

        cached = self._base_strips_cache
        return cached is not None and not cached[2]

    def render_strips(
        self,
        width: int,
        height: int | None,
        style: Style,
        options: RenderOptions,
    ) -> list[Strip]:
        base_strips = self._base_strips(width, style, options)
        strips = base_strips if height is None else base_strips[:height]
        # Selection offsets address the rendered rows, not the Markdown source:
        # bullets, code chrome, and soft wrapping must match what was highlighted.
        self.plain = "\n".join(strip.text.rstrip() for strip in strips)
        selection = options.selection
        selection_style = options.selection_style
        rendered: list[Strip] = []
        for y, strip in enumerate(strips):
            if selection is not None and selection_style is not None:
                span = selection.get_span(y)
                if span is not None:
                    start, end = span
                    line_length = len(strip.text)
                    if not line_length:
                        rendered.append(strip)
                        continue
                    start = min(max(0, start), line_length)
                    end = line_length if end < 0 else min(max(start, end), line_length)
                    start_cell = strip.index_to_cell_position(start)
                    end_cell = strip.index_to_cell_position(end)
                    strip = Strip.join(
                        [
                            strip.crop(0, start_cell),
                            self._apply_selection_style(
                                strip.crop(start_cell, end_cell),
                                style,
                                selection_style,
                            ),
                            strip.crop(end_cell),
                        ]
                    )
            # Textual's compositor uses this metadata to translate a pointer cell
            # back to the character offset consumed by ``Selection.extract``.
            rendered.append(strip.apply_offsets(0, y))
        return rendered


class StreamMessage(Static):
    """One assistant turn rendered as Rich Markdown in a single Textual widget."""

    DEFAULT_CSS = """
    StreamMessage {
        width: 1fr;
        max-width: 100%;
        height: auto;
        overflow-x: hidden;
        color: $text;
        link-color: $text-primary;
        link-color-hover: $text-accent;
        link-style: underline;
        link-style-hover: bold underline;
    }
    """

    def __init__(self, initial_markdown: str | None = None) -> None:
        super().__init__(Content(), markup=False)
        self._source = initial_markdown or ""
        self._render_failed = False
        self._selection_visual: _SelectableMarkdownVisual | None = None
        self._incremental_markdown = IncrementalMarkdownState()
        self._markdown_render_config: _MarkdownRenderConfig | None = None
        self._code_block_render_cache: _CodeBlockRenderCache = {}
        self._last_markdown_processed_chars = 0
        self._last_markdown_reused_chars = 0
        self._last_markdown_incremental = False
        self.add_class("message", "message--assistant")

    @property
    def source(self) -> str:
        """Return the authoritative Markdown source currently represented."""

        return self._source

    @property
    def has_measured_empty_render(self) -> bool:
        """Whether this document completed layout with no visible rows."""

        visual = self._selection_visual
        return visual is not None and visual.has_measured_empty_render

    def needs_reconciliation(self, content: str) -> bool:
        """Return whether authoritative content requires another Markdown render."""

        return self._render_failed or self._source != content

    def on_mount(self) -> None:
        if self._source:
            self._render_source()

    def notify_style_update(self) -> None:
        super().notify_style_update()
        self._markdown_render_config = None
        self._code_block_render_cache.clear()
        if self.is_mounted and self._source:
            self._render_source()

    def _size_updated(
        self,
        size: Size,
        virtual_size: Size,
        container_size: Size,
        layout: bool = True,
    ) -> bool:
        """Apply measured geometry without scheduling a recursive layout pass.

        Updating this auto-height widget already requested the layout that measured
        these dimensions. Textual's default implementation assigns ``virtual_size``
        through its layout-reactive descriptor, which schedules a second full screen
        layout even though the compositor just produced the value. A direct reactive
        update retains the measured geometry and scroll bookkeeping while leaving
        future content updates and terminal resizes responsible for their own layout.
        """

        return super()._size_updated(size, virtual_size, container_size, layout=False)

    async def append_markdown(self, fragment: str) -> None:
        """Append a coalesced fragment while reusing complete Markdown blocks."""

        self._source += fragment
        self._render_incremental_source()

    async def replace_markdown(self, content: str) -> None:
        """Replace the document from authoritative completion or history content."""

        self._source = content
        self._render_source()

    def release_streaming_markdown_caches(self) -> None:
        """Release parser and highlighting state no longer needed after settlement."""

        self._incremental_markdown.release()
        self._code_block_render_cache.clear()
        visual = self._selection_visual
        if visual is None:
            return
        renderable = visual._markdown_renderable
        if isinstance(renderable, _SafeAssistantMarkdown):
            renderable.markdown.release_render_cache()

    @property
    def last_markdown_processed_chars(self) -> int:
        """Source characters parsed and sanitized by the latest write."""

        return self._last_markdown_processed_chars

    @property
    def last_markdown_reused_chars(self) -> int:
        """Stable source characters represented by retained tokens."""

        return self._last_markdown_reused_chars

    @property
    def last_markdown_incremental(self) -> bool:
        """Whether the latest write used the guarded stable-prefix path."""

        return self._last_markdown_incremental

    def action_open_markdown_link(self, href: str) -> None:
        """Open a Rich Markdown hyperlink through Textual's application boundary."""

        self.app.open_url(href)

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Extract the selected rendered Markdown text for Textual's copy action."""

        visual = self._selection_visual
        if visual is None:
            return super().get_selection(selection)
        return selection.extract(visual.plain), "\n"

    def _render_source(self) -> None:
        self._incremental_markdown.reset()
        self._code_block_render_cache.clear()
        self._last_markdown_processed_chars = len(self._source)
        self._last_markdown_reused_chars = 0
        self._last_markdown_incremental = False
        try:
            markdown = self._build_markdown(self._source)
        except Exception as error:
            self._show_markdown_fallback(error)
            return
        self._show_markdown(markdown)

    def _render_incremental_source(self) -> None:
        try:
            build = self._incremental_markdown.build(self._source, self._build_markdown)
            markdown = build.markdown
            if not isinstance(markdown, _AssistantMarkdown):
                raise TypeError("Markdown builder returned an unsupported renderable")
            if not build.incremental:
                self._code_block_render_cache.clear()
            markdown.enable_stable_fence_cache(
                build.cacheable_fence_token_ids,
                self._code_block_render_cache,
            )
        except Exception as error:
            self._incremental_markdown.reset()
            self._code_block_render_cache.clear()
            self._last_markdown_processed_chars = len(self._source)
            self._last_markdown_reused_chars = 0
            self._last_markdown_incremental = False
            self._show_markdown_fallback(error)
            return
        self._last_markdown_processed_chars = build.processed_chars
        self._last_markdown_reused_chars = build.reused_chars
        self._last_markdown_incremental = build.incremental
        self._show_markdown(markdown)

    def _show_markdown(self, markdown: _AssistantMarkdown) -> None:
        self._render_failed = False
        visual = _SelectableMarkdownVisual(
            self,
            _SafeAssistantMarkdown(self._source, markdown),
        )
        self._selection_visual = visual
        self.update(visual)

    def _show_markdown_fallback(self, error: Exception) -> None:
        if not self._render_failed:
            self.log.error(f"Markdown rendering failed; using literal fallback: {error}")
        self._render_failed = True
        self._selection_visual = None
        self.update(Content(self._source))

    def _build_markdown(self, source: str) -> _AssistantMarkdown:
        config = self._markdown_render_config
        if config is None:
            config = self._build_markdown_render_config()
            self._markdown_render_config = config
        return _AssistantMarkdown(
            _sanitize_markdown_controls(source),
            theme=config.theme,
            code_theme=config.code_theme,
            native_ansi=config.native_ansi,
        )

    def _build_markdown_render_config(self) -> _MarkdownRenderConfig:
        theme = self.app.current_theme
        foreground = theme.foreground or "default"
        secondary = theme.secondary or foreground
        accent = theme.accent or foreground
        panel = theme.panel or "default"
        heading = theme.warning or foreground
        styles = {
            "markdown.paragraph": foreground,
            "markdown.text": foreground,
            "markdown.strong": f"bold {foreground}",
            "markdown.code": f"bold {accent} on {panel}",
            "markdown.code_block": foreground,
            "markdown.block_quote": secondary,
            "markdown.list": accent,
            "markdown.item": foreground,
            "markdown.item.bullet": f"bold {accent}",
            "markdown.item.number": accent,
            "markdown.hr": secondary,
            "markdown.h1": f"bold underline {heading}",
            **{f"markdown.h{level}": f"bold {heading}" for level in range(2, 7)},
            "markdown.link": f"underline {foreground}",
            "markdown.link_url": f"underline {foreground}",
            "markdown.table.border": secondary,
            "markdown.table.header": f"bold {foreground}",
        }
        return _MarkdownRenderConfig(
            theme=RichTheme(styles),
            code_theme="monokai" if theme.dark else "friendly",
            native_ansi=self.app.native_ansi_color,
        )


def _sanitize_markdown_controls(source: str) -> str:
    """Strip terminal controls except inside an explicitly tagged ANSI fence."""

    fence_character: str | None = None
    fence_length = 0
    ansi_fence = False
    sanitized: list[str] = []
    for line in source.splitlines(keepends=True):
        marker = _MARKDOWN_FENCE_RE.match(line)
        if fence_character is None and marker is not None:
            delimiter, info = marker.groups()
            fence_character = delimiter[0]
            fence_length = len(delimiter)
            stripped_info = info.strip()
            ansi_fence = bool(
                stripped_info and stripped_info.split(maxsplit=1)[0].lower() == "ansi"
            )
            sanitized.append(line)
            continue
        if fence_character is not None and marker is not None:
            delimiter, info = marker.groups()
            if (
                delimiter[0] == fence_character
                and len(delimiter) >= fence_length
                and not info.strip()
            ):
                fence_character = None
                fence_length = 0
                ansi_fence = False
                sanitized.append(line)
                continue
        sanitized.append(line if ansi_fence else Text.from_ansi(line).plain)
    return "".join(sanitized)
