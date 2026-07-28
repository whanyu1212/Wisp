"""Per-message transcript widgets for the Textual TUI.

Stage 2 replaces the append-only ``RichLog`` transcript with a
``VerticalScroll`` of these widgets, one per turn/event. Two kinds:

- ``LineMessage`` — a role-styled single block for tool calls, results,
  approvals, errors, notices, and user input. Content is escaped Rich markup in
  a ``Static`` (never fed to the Markdown parser), preserving the
  escape-at-boundary invariant for untrusted tool/error payloads.
- ``StreamMessage`` — the streaming assistant turn, backed by a ``Markdown``
  widget so model output renders code blocks, lists, and emphasis. Its content
  is driven from an authoritative text buffer via ``set_content`` and reconciled
  with one coalesced refresh (see ``TextualTui`` streaming), which avoids the
  mount race where ``update``/``append`` on a not-yet-mounted widget drops text.
"""

from __future__ import annotations

import time
from collections.abc import Mapping

from textual import events
from textual.app import ComposeResult
from textual.await_complete import AwaitComplete
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.timer import Timer
from textual.widget import AwaitMount, Widget
from textual.widgets import Input, Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from wisp.events import RpcSessionSummary, ToolApprovalRequested, TrustRequested
from wisp.providers.catalog import ModelCatalogProviderEntry
from wisp.runtime.commands import CommandDescriptor
from wisp.tui.command_palette import search_command_catalog
from wisp.tui.commands import (
    DEFAULT_TUI_COMMAND_CATALOG,
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
from wisp.tui.rendering import (
    TuiViewSnapshot,
    _truncate_to_cell_width,
    format_tui_footer_text,
)

_TOOL_OUTPUT_PREVIEW_LINES = 8
_TOOL_OUTPUT_PREVIEW_BYTES = 2_000
PASTE_DISPLAY_THRESHOLD = 2_000


class PromptEditor(TextArea):
    """Multiline prompt editor with Pi-compatible submission keys."""

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
        """Submit on Enter and reserve Pi's newline keys for multiline input."""

        if event.key == "enter":
            event.stop()
            event.prevent_default()
            # Full expansion for the model; the raw editor text (placeholders
            # intact) for the compact transcript echo.
            self.post_message(self.Submitted(self.text_for_submission(), self.text))
        elif event.key in {"shift+enter", "ctrl+j"}:
            event.stop()
            event.prevent_default()
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


def _has_detail(detail: str | Content) -> bool:
    """Whether a card detail carries content, for both str and Content forms.

    An empty ``str`` and an empty ``Content`` both mean "no detail", so a
    degenerate render never overwrites a real prior detail or forces an empty
    block. A ``Content`` is truthy by identity, so check its text explicitly.
    """

    if isinstance(detail, Content):
        return bool(detail.plain)
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


def _summarize_arguments(arguments: object, *, limit: int = 48) -> str:
    """Render a tool call's arguments as a terse `k=v, k=v` summary.

    Values are stringified and clipped so a card stays one line; a long single
    value (a pasted blob, a big path) is truncated with an ellipsis rather than
    wrapping the card. Non-mapping arguments fall back to their repr.
    """

    if not isinstance(arguments, Mapping):
        text = str(arguments)
        return text if len(text) <= limit else f"{text[: limit - 1]}…"
    parts: list[str] = []
    for key, value in arguments.items():
        text = str(value)
        if len(text) > limit:
            text = f"{text[: limit - 1]}…"
        parts.append(f"{key}={text}")
    return ", ".join(parts)


class DecisionPanel(Vertical):
    """Approval/trust selector that temporarily replaces the composer.

    The main approval prompt defaults its highlight to "Approve once" (Enter
    approves); the YOLO-confirmation and trust prompts remain deny-first
    (Enter/Escape decline). Escape always denies/cancels on every panel,
    regardless of which option is highlighted.
    """

    DEFAULT_CSS = """
    DecisionPanel {
        display: none;
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
        key = event.key.lower()
        answer: str | None = None
        if self._mode == "approval":
            answer = {"1": "y", "2": "t", "3": "a", "4": "n", "escape": "n"}.get(key)
        elif self._mode == "all_confirmation":
            answer = {
                "1": "confirm-all",
                "2": "cancel-all",
                "escape": "cancel-all",
            }.get(key)
        elif self._mode == "trust":
            answer = {"1": "y", "2": "n", "escape": "n"}.get(key)
        if answer is None:
            return
        self.submit_answer(answer)
        event.prevent_default()
        event.stop()


class ModelPicker(Vertical):
    """Interactive `/model` selector: pick a model, arrow-adjust its effort.

    A single unified list (Claude Code style), not two separate screens —
    left/right arrow keys cycle an effort tier for whichever model row is
    currently highlighted, when that model has one (``effort_levels`` in the
    catalog; a model with no entry there shows no effort control at all).
    Confirming posts a synthesized ``/model <id> [effort]`` command text
    through the same answer-is-a-text-line mechanism `DecisionPanel` already
    uses for approval/trust — reusing 100% of the existing prompt-submission
    plumbing rather than adding a second, structured return path.
    """

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
        height: 1;
        color: $text-muted;
        padding: 0 1;
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
        self._effort_line = Static("", id="model-picker-effort", markup=False)
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
        yield self._effort_line

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
        self._update_effort_line()
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

    def _update_effort_line(self) -> None:
        row = self._highlighted_row()
        if row is None:
            self._effort_line.update("")
            return
        levels = self._effort_levels_for(row)
        if not levels:
            self._effort_line.update("")
            return
        chosen = self._effort_choice.get(row)
        parts = []
        for level in levels:
            parts.append(f"[{level}]" if level == chosen else level)
        prefix = "(default)" if chosen is None else ""
        line = "effort:  " + "  ".join(([prefix] if prefix else []) + parts)
        self._effort_line.update(line.strip())

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
        self._update_effort_line()

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
        self._update_effort_line()

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
        if event.key == "left":
            self.cycle_effort(direction=-1)
            event.prevent_default()
            event.stop()
        elif event.key == "right":
            self.cycle_effort(direction=1)
            event.prevent_default()
            event.stop()
        elif event.key == "escape":
            self.post_message(self.Cancelled())
            event.prevent_default()
            event.stop()


class SessionPicker(Vertical):
    """Interactive newest-first RPC session selector used by bare ``/resume``."""

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

    SessionPicker #session-picker-options {
        height: auto;
        max-height: 15;
        border: none;
        background: transparent;
        padding: 0;
    }

    SessionPicker #session-picker-options > .option-list--option-highlighted {
        background: $accent 30%;
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
        self._hint = Static(
            "enter select · esc cancel",
            id="session-picker-hint",
            markup=False,
        )
        self._rows: list[str] = []
        self._submitted = False
        self._opened_at = 0.0

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._options
        yield self._hint

    @property
    def is_open(self) -> bool:
        return self.display

    def focus_options(self) -> None:
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

    def show(
        self,
        sessions: tuple[RpcSessionSummary, ...],
        *,
        selected_session_id: str | None,
    ) -> None:
        self._submitted = False
        self._opened_at = time.monotonic()
        self._options.clear_options()
        self._rows = []
        selected_index: int | None = None
        for index, session in enumerate(sessions):
            current = session.session_id == selected_session_id
            marker = "●" if current else " "
            name = _truncate_to_cell_width(session.name or session.session_id[:12], 48)
            updated = session.updated_at.isoformat(timespec="minutes")
            path = _truncate_to_cell_width(str(session.session_path), 72)
            label = f"{marker} {name} · {session.entry_count} entries · {updated}\n  {path}"
            # Persisted names and paths are untrusted display text. Content()
            # keeps bracket syntax literal instead of letting Option parse it as
            # Textual markup.
            self._options.add_option(Option(Content(label), id=session.session_id))
            self._rows.append(session.session_id)
            if current:
                selected_index = index
        if sessions:
            self._options.highlighted = selected_index if selected_index is not None else 0
            self._hint.update("enter select · esc cancel")
        else:
            self._options.add_option(Option("No persisted sessions found.", disabled=True))
            self._hint.update("esc close")
        self.display = True
        self.focus_options()

    def hide(self) -> None:
        self.display = False
        self._submitted = False

    def submit_current_selection(self) -> None:
        if self._submitted or not self.is_open:
            return
        highlighted = self._options.highlighted
        if highlighted is None or highlighted >= len(self._rows):
            return
        self._submitted = True
        self.post_message(self.Selected(self._rows[highlighted]))

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
        if event.key == "escape":
            self.post_message(self.Cancelled())
            event.prevent_default()
            event.stop()
        elif event.key == "pageup":
            self._options.action_page_up()  # type: ignore[no-untyped-call]
            event.prevent_default()
            event.stop()
        elif event.key == "pagedown":
            self._options.action_page_down()  # type: ignore[no-untyped-call]
            event.prevent_default()
            event.stop()
        elif event.key == "home":
            self._options.action_first()
            event.prevent_default()
            event.stop()
        elif event.key == "end":
            self._options.action_last()
            event.prevent_default()
            event.stop()


class TranscriptEmptyState(Vertical):
    """Centered identity shown only while the transcript has no output.

    Just a wordmark line plus a hint line, both given matching explicit
    widths (set on ``.styles``, not fixed CSS) rather than relying on
    Textual's ``align: center middle`` to center each child independently —
    empirically it doesn't: siblings under it share a left edge, not a
    center, so two boxes of different widths end up with visibly different
    true centers (confirmed by a probe: two Statics at width 51 vs 40 landed
    at x=14 both, centers 39 vs 34). Matching widths is the only combination
    that produces matching centers.

    Below all four of Wisp's supported breakpoints (72x20 and up), this
    already fits with room to spare, so the hint-hiding below is a defensive
    floor for smaller-than-spec terminals, not the primary responsive
    mechanism for issue #72 — it just avoids the hint line ever crowding or
    clipping against the wordmark on an unusually short screen.
    """

    DEFAULT_CSS = """
    TranscriptEmptyState.-compact #transcript-empty-hint {
        display: none;
    }
    """

    def __init__(self, wordmark: str, hint: str) -> None:
        super().__init__(id="transcript-empty")
        self._wordmark = wordmark
        self._hint = hint

    def compose(self) -> ComposeResult:
        wordmark_static = Static(
            self._wordmark,
            id="transcript-empty-wordmark",
            markup=False,
        )
        wordmark_static.styles.width = 40
        yield wordmark_static
        hint_static = Static(
            self._hint,
            id="transcript-empty-hint",
            markup=False,
        )
        hint_static.styles.width = 40
        yield hint_static

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(self.size.height < 5, "-compact")


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

    class FollowChanged(Message):
        """The viewport entered or left sticky tail-follow mode."""

        def __init__(self, following: bool) -> None:
            super().__init__()
            self.following = following

    def __init__(
        self,
        *args: object,
        empty_wordmark: str | None = None,
        empty_hint: str = "",
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._follow = True
        self._empty_wordmark = empty_wordmark
        self._empty_hint = empty_hint
        self._empty_state: TranscriptEmptyState | None = None

    def compose(self) -> ComposeResult:
        if self._empty_wordmark is not None:
            self._empty_state = TranscriptEmptyState(self._empty_wordmark, self._empty_hint)
            yield self._empty_state

    def mount_message(self, widget: Widget) -> AwaitMount:
        """Mount output after permanently dismissing the initial empty state."""

        empty_state = self._empty_state
        if empty_state is not None:
            self._empty_state = None
            empty_state.display = False
            empty_state.remove()
        return self.mount(widget)

    def clear_messages(self) -> None:
        """Remove every mounted transcript item and restore tail-follow state."""

        self._empty_state = None
        self._follow = True
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
            self.post_message(self.FollowChanged(self._follow))

    @property
    def is_following(self) -> bool:
        """Whether new output should remain pinned to the transcript tail."""

        return self._follow

    def follow_tail(self) -> None:
        """Scroll to the newest content iff the user hasn't scrolled away."""
        if self._follow:
            self.scroll_end(animate=False)

    def return_to_latest(self) -> None:
        """Restore tail-follow intent and jump to the newest output immediately."""

        was_following = self._follow
        self._follow = True
        self.scroll_end(animate=False)
        if not was_following:
            self.post_message(self.FollowChanged(True))


class CommandPalette(Vertical):
    """Searchable overlay backed by the TUI's executable command catalog."""

    DEFAULT_CSS = """
    CommandPalette {
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

    CommandPalette #command-palette-query {
        height: 3;
        border: none;
        border-bottom: solid $secondary;
        background: transparent;
        padding: 0 1;
    }

    CommandPalette #command-palette-options {
        height: auto;
        max-height: 12;
        border: none;
        background: transparent;
        padding: 0;
        scrollbar-size-vertical: 1;
    }

    CommandPalette #command-palette-options > .option-list--option-highlighted {
        background: $accent 30%;
    }

    CommandPalette #command-palette-hint {
        height: 1;
        color: $text-muted;
        text-align: right;
        text-style: dim;
    }
    """

    class Selected(Message):
        """One command explicitly selected from the palette."""

        def __init__(self, descriptor: CommandDescriptor) -> None:
            super().__init__()
            self.descriptor = descriptor

    class Cancelled(Message):
        """The palette was dismissed without invoking a command."""

    def __init__(self, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id)
        self._query = Input(placeholder="Search commands", id="command-palette-query")
        self._options = OptionList(id="command-palette-options")
        self._hint = Static(
            "↑↓ navigate · enter run · esc cancel",
            id="command-palette-hint",
            markup=False,
        )
        self._catalog = DEFAULT_TUI_COMMAND_CATALOG
        self._visible: tuple[CommandDescriptor, ...] = ()
        self._submitted = False
        self._opened_at = 0.0

    def compose(self) -> ComposeResult:
        yield self._query
        yield self._options
        yield self._hint

    @property
    def is_open(self) -> bool:
        return self.display

    def set_catalog(self, catalog: TuiCommandCatalog) -> None:
        self._catalog = catalog
        if self.is_open:
            self._refresh_options()

    def move_highlight_page_up(self) -> None:
        self._options.action_page_up()  # type: ignore[no-untyped-call]

    def move_highlight_page_down(self) -> None:
        self._options.action_page_down()  # type: ignore[no-untyped-call]

    def move_highlight_first(self) -> None:
        self._options.action_first()

    def move_highlight_last(self) -> None:
        self._options.action_last()

    def show(self) -> None:
        self._submitted = False
        self._opened_at = time.monotonic()
        self._query.value = ""
        self.display = True
        self._refresh_options()
        self._query.focus()

    def hide(self) -> None:
        self.display = False
        self._submitted = False

    def _refresh_options(self) -> None:
        matches = search_command_catalog(self._catalog, self._query.value)
        self._visible = tuple(match.descriptor for match in matches)
        self._options.clear_options()
        for descriptor in self._visible:
            self._options.add_option(
                Option(
                    Content(self._option_label(descriptor)),
                    id=descriptor.name,
                )
            )
        if self._visible:
            self._options.highlighted = 0
            self._hint.update("↑↓ navigate · enter run · esc cancel")
        else:
            self._options.add_option(Option("No matching commands.", disabled=True))
            self._hint.update("esc close")

    @staticmethod
    def _option_label(descriptor: CommandDescriptor) -> str:
        arguments = " ".join(
            f"<{argument.name}>" if argument.required else f"[{argument.name}]"
            for argument in descriptor.arguments
        )
        invocation = descriptor.slash_command
        if arguments:
            invocation = f"{invocation} {arguments}"
        aliases = ", ".join(descriptor.slash_aliases)
        alias_text = f" · aliases {aliases}" if aliases else ""
        return (
            f"{descriptor.title}  {invocation} · {descriptor.category}\n"
            f"  {descriptor.description}{alias_text}"
        )

    def _move(self, action: str) -> None:
        getattr(self._options, action)()

    def submit_current_selection(self) -> None:
        if self._submitted or not self.is_open:
            return
        highlighted = self._options.highlighted
        if highlighted is None or highlighted >= len(self._visible):
            return
        self._submitted = True
        self.post_message(self.Selected(self._visible[highlighted]))

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
            self._move(action)
            event.prevent_default()
            event.stop()
        elif event.key == "enter":
            self.submit_current_selection()
            event.prevent_default()
            event.stop()
        elif event.key == "escape":
            self.post_message(self.Cancelled())
            event.prevent_default()
            event.stop()


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

    # overlay: screen floats the menu over the transcript WITHOUT reflowing it,
    # while keeping its natural compose position (just above #input, where it's
    # yielded). It is deliberately NOT put on a separate `layer:` — a lone child on
    # the overlay layer gets laid out at the TOP of the app by that layer's own
    # vertical layout, detaching it from the prompt (the bug Codex caught).
    # constrain: inside keeps it fully on-screen at any terminal size.
    DEFAULT_CSS = """
    SlashSuggest {
        overlay: screen;
        constrain: inside;
        display: none;
        width: auto;
        max-width: 60;
        height: auto;
        max-height: 8;
        border: round $accent;
        background: $panel;
        padding: 0 1;
        scrollbar-size-vertical: 1;
    }
    SlashSuggest > .option-list--option-highlighted {
        background: $accent 30%;
    }
    """

    # CSS max-width ceiling on wide terminals (DEFAULT_CSS above); on_resize
    # narrows self.styles.max_width below this only when the screen itself
    # can't fit it. Tracked here too (not read back from styles.max_width,
    # a Scalar) so show_for's column-alignment truncation has a plain int
    # content-width budget to compute against.
    _MAX_WIDTH_CEILING = 60

    def __init__(self, id: str | None = None) -> None:  # noqa: A002 - Textual's param name
        super().__init__(id=id)
        self._specs = SLASH_COMMAND_SPECS
        # spelling → spec, so the highlighted option's id maps back to its command.
        self._by_command: dict[str, SlashCommandSpec] = {spec.command: spec for spec in self._specs}
        self._visible_specs: tuple[SlashCommandSpec, ...] = ()
        self._max_width = self._MAX_WIDTH_CEILING

    def set_catalog(self, catalog: TuiCommandCatalog) -> None:
        self._specs = catalog.specs
        self._by_command = {spec.command: spec for spec in self._specs}
        self.hide()

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

        return tuple(spec for spec in self._specs if spec.command.startswith(query))

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
                    _truncate_to_cell_width(
                        f"{spec.command:<{name_width}}  {spec.description}", content_width
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


# CSS role classes are applied per message so Stage 3 can style cards purely in
# CSS; the role also names the border_title label — with one exception:
# ToolCard.set_state() checks its own _STATUS_LABELS (below _STATUS on the
# class) before falling back to this dict by role, since it intentionally
# reuses the "denied" CSS role class for both "denied" and "cancelled" (same
# left-rule color and glyph family — both mean "stopped by a decision, not a
# failure") while still needing their border titles to read differently.
_ROLE_LABELS: dict[str, str] = {
    "user": "you",
    "assistant": "assistant",
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
        # `markup` is already-composed Rich markup (label styled, payload escaped
        # by the caller). Static renders it with markup enabled by default.
        super().__init__(markup)
        self.add_class("message", f"message--{role}")
        # The role label is a fixed literal from _ROLE_LABELS — never untrusted
        # payload — so it's safe as border chrome. Quiet meta roles (dim/session)
        # map to "" and get no title, staying borderless per the card CSS.
        label = _ROLE_LABELS.get(role, "")
        if label:
            self.border_title = label


class ToolCard(Static):
    """One evolving transcript card for a single tool call, keyed by call_id.

    A tool call emits up to three events sharing a call_id — request, an optional
    approval resolution (only for safety-gated tools), and a result. Rather than
    mint a separate line per event, one ``ToolCard`` is mounted on the request and
    then *mutated in place* as the later events arrive. The card carries its status
    in a leading glyph plus the role CSS class (which colors the left rule), so the
    whole lifecycle reads as one card transitioning pending → running → done/error
    instead of three stacked cards the reader has to reconcile. Resolved cards add
    a bounded multiline output preview below their compact status row.

    Parallel calls each own a stable card regardless of finish order, because the
    registry (in ``TextualTui``) routes every event to the card for its call_id.
    """

    # status → (leading glyph, role class). The role class drives the left-rule
    # color via the shared `.message--{role}` CSS in TextualTui.
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

    def __init__(self, name: str, arguments: object) -> None:
        super().__init__("")
        # Not `_name`: Textual's DOMNode uses `self._name` to back the widget
        # `name` property (typed str | None), so a distinct field avoids
        # shadowing it and keeps this a plain str.
        self._tool_name = name
        self._summary = _summarize_arguments(arguments)
        # A plain str is untrusted output escaped at repaint; a Content is an
        # already-styled renderable (e.g. a colored diff) whose text is literal,
        # so it is composed directly without markup escaping.
        self._detail: str | Content = ""
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

    def on_unmount(self) -> None:
        self._stop_timer()

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _tick(self) -> None:
        self._elapsed = (self._elapsed or 0.0) + self._TICK
        self._repaint()

    def set_state(
        self,
        status: str,
        *,
        detail: str | Content = "",
        elapsed: float | None = None,
        full_output: str = "",
        truncated: bool = False,
    ) -> None:
        """Transition the card to a new status, swapping glyph, color, and detail.

        ``detail`` overrides the argument summary (used to show a denial reason or
        bounded result preview). A plain ``str`` is untrusted output escaped at
        repaint; a Textual ``Content`` is a pre-styled renderable (e.g. a colored
        diff) composed directly. ``elapsed`` is the true wall-clock duration (from
        the request/result event timestamps); passing it freezes the live counter
        at the honest value and stops the per-card timer. ``full_output`` is the
        tool's full (tool-bounded) output, retained so the reader can expand past the
        collapsed detail; ``truncated`` says the tool itself capped that output. The
        role CSS class is swapped rather than added so the left-rule color reflects
        only the current state.
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

    def _can_expand(self) -> bool:
        """Whether expanding would show anything the collapsed detail doesn't.

        True only when there is full output AND it differs from what the collapsed
        detail already shows — a short output whose preview is the whole thing, or a
        card with no retained output (pending, denied, error message), has nothing to
        expand, so its toggle is a no-op and no affordance is shown.
        """

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

    def _repaint(self) -> None:
        # Build the whole card as Content, appending every untrusted value
        # (name, summary, detail) as LITERAL styled text. Nothing untrusted is
        # ever routed through a markup parser, so no escaping is needed and no
        # content — however it is truncated or whatever brackets it contains —
        # can inject or break a style span. Trusted chrome (glyph, `·`, indent)
        # is plain literal too; styles are applied out-of-band.
        content = Content(f"{self._glyph} ") + Content.styled(self._tool_name, "b")
        if not _has_detail(self._detail) and self._summary:
            content += Content("  ") + Content.styled(self._summary, "dim")
        if self._elapsed is not None:
            content += Content.styled(f" · {_format_duration(self._elapsed)}", "dim")
        # A ▸/▾ affordance signals the card can be expanded and its current state,
        # shown only when there is genuinely more to reveal than the collapsed detail.
        if self._can_expand():
            content += Content.styled(" ▾" if self._expanded else " ▸", "dim")

        if self._expanded and self._full_output:
            # Expanded: show the full (tool-bounded) output in place of the collapsed
            # detail, so the reader sees what the preview/summary/diff stood in for.
            content += Content("\n") + self._indent_str(self._full_output)
        elif isinstance(self._detail, Content):
            # A pre-styled renderable (e.g. a colored diff): compose its already
            # theme-styled, literal text directly, indented under the status row.
            content += Content("\n") + _indent_content(self._detail)
        elif self._detail:
            content += Content("\n") + self._indent_str(self._detail)

        if self._truncated:
            # The tool capped its own output before it ever reached here, so what the
            # card shows — collapsed preview or expanded full output — isn't the whole
            # story. Say so honestly regardless of expand state: a capped output that
            # fits the preview budget (so there's nothing extra to expand) would
            # otherwise present as complete, which is exactly the case this marks.
            content += Content("\n") + Content.styled(
                "  ⋯ output truncated at the tool's limit", "dim"
            )

        self.update(content)

    @staticmethod
    def _indent_str(text: str) -> Content:
        """Indent untrusted output two spaces and style it dim, as literal text."""

        indented = "\n".join(f"  {line}" for line in text.split("\n"))
        return Content.styled(indented, "dim")


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
        self.update(text)


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


class StreamMessage(Widget):
    """The streaming assistant turn, backed by a Markdown widget.

    Content is set from an external authoritative buffer; the widget never
    accumulates deltas itself, so it is safe against the mount race.
    """

    DEFAULT_CSS = """
    StreamMessage {
        height: auto;
    }
    StreamMessage > Markdown {
        height: auto;
        margin: 0;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.add_class("message", "message--assistant")
        # Match the finalized-assistant card so the streamed and settled turns
        # look identical (same role label + card CSS).
        self.border_title = _ROLE_LABELS["assistant"]
        self._markdown = Markdown()

    def compose(self) -> ComposeResult:
        yield self._markdown

    def set_content(self, text: str) -> AwaitComplete:
        # Reconcile the Markdown to the authoritative buffer and return update()'s
        # AwaitComplete, which resolves once *this update's* block children have
        # mounted (batched, under a lock). The caller awaits it before following
        # the tail so the scroll lands on the fully-laid-out extent rather than a
        # partially-mounted one.
        #
        # Also keep Markdown's own _initial_markdown in sync: Markdown._on_mount
        # runs `update(self._initial_markdown or "")` on its Mount event, which is
        # a *separate* async path from this call. If a turn is finalized in the
        # same tick the widget mounts (delta then flush with no refresh between),
        # that mount can run after our update() and clobber the content back to "".
        # Seeding _initial_markdown means whichever path runs last applies our text.
        self._markdown._initial_markdown = text
        return self._markdown.update(text)
