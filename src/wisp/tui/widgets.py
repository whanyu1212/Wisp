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

import json
from collections.abc import Mapping
from dataclasses import dataclass

from textual import events
from textual.app import ComposeResult
from textual.await_complete import AwaitComplete
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.timer import Timer
from textual.widget import AwaitMount, Widget
from textual.widgets import Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from wisp.events import ToolApprovalRequested, TrustRequested
from wisp.tui.commands import SLASH_COMMAND_SPECS, SlashCommandSpec
from wisp.tui.rendering import (
    TuiViewSnapshot,
    _format_cwd_for_footer,
    _markup_escape,
    format_tui_footer_text,
)

_DECISION_PREVIEW_LINES = 5
_DECISION_PREVIEW_CHARS = 320
_TOOL_OUTPUT_PREVIEW_LINES = 8
_TOOL_OUTPUT_PREVIEW_BYTES = 2_000
PASTE_DISPLAY_THRESHOLD = 2_000


class PromptEditor(TextArea):
    """Multiline prompt editor with Pi-compatible submission keys."""

    class Submitted(Message):
        """The complete prompt accepted by the editor."""

        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

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
        self.insert(placeholder)

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

    def sync_pending_paste(self) -> None:
        """Invalidate stored paste content when its placeholder is edited away."""

        self._pending_pastes = [
            (placeholder, content)
            for placeholder, content in self._pending_pastes
            if placeholder in self.text
        ]

    def text_for_submission(self) -> str:
        """Return the prompt text, expanding intact large-paste placeholders."""

        self.sync_pending_paste()
        text = self.text
        for placeholder, content in self._pending_pastes:
            text = text.replace(placeholder, content, 1)
        return text

    async def on_key(self, event: events.Key) -> None:
        """Submit on Enter and reserve Pi's newline keys for multiline input."""

        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self.text_for_submission()))
        elif event.key in {"shift+enter", "ctrl+j"}:
            event.stop()
            event.prevent_default()
            self.insert("\n")


@dataclass(frozen=True)
class _DecisionContent:
    title: str
    meta: str
    detail: str


def _bounded_decision_preview(
    lines: list[str],
    *,
    max_lines: int = _DECISION_PREVIEW_LINES,
    max_chars: int = _DECISION_PREVIEW_CHARS,
) -> str:
    """Return a compact preview with an explicit final truncation marker."""

    normalized: list[str] = []
    for line in lines:
        normalized.extend(line.replace("\r\n", "\n").replace("\r", "\n").split("\n"))

    selected: list[str] = []
    used_chars = 0
    truncated = False
    for line in normalized:
        if len(selected) >= max_lines or used_chars >= max_chars:
            truncated = True
            break
        remaining = max_chars - used_chars
        if len(line) > remaining:
            selected.append(line[:remaining])
            used_chars += remaining
            truncated = True
            break
        selected.append(line)
        used_chars += len(line)

    if len(selected) < len(normalized):
        truncated = True
    if not selected:
        selected.append("")
    if truncated:
        if len(selected) >= max_lines:
            selected[-1] = "... preview truncated"
        else:
            selected.append("... preview truncated")
    return "\n".join(selected)


def _safety_label(safety: str) -> str:
    return {
        "read": "read-only access",
        "mutating": "file mutation",
        "command": "command execution",
    }.get(safety, safety)


def _approval_content(event: ToolApprovalRequested, *, cwd: str) -> _DecisionContent:
    arguments = event.arguments
    cwd_text = _format_cwd_for_footer(cwd)
    safety = _safety_label(event.safety)

    if event.name == "bash":
        command = arguments.get("command")
        command_text = command if isinstance(command, str) else ""
        lines = [
            f"$ {line}" if index == 0 else f"  {line}"
            for index, line in enumerate(command_text.splitlines() or [""])
        ]
        timeout = arguments.get("timeout")
        if timeout is not None:
            lines.append(f"timeout: {timeout}s")
        return _DecisionContent(
            title="Run command?",
            meta=f"bash - {safety}\ncwd: {cwd_text}",
            detail=_bounded_decision_preview(lines),
        )

    if event.name == "write":
        path = arguments.get("path")
        path_text = path if isinstance(path, str) else "unknown path"
        content = arguments.get("content")
        content_text = content if isinstance(content, str) else ""
        line_count = len(content_text.splitlines())
        byte_count = len(content_text.encode("utf-8"))
        lines = [f"content: {line_count} lines, {byte_count} bytes"]
        lines.extend(content_text.splitlines())
        return _DecisionContent(
            title="Write file?",
            meta=f"{path_text}\n{safety} - cwd: {cwd_text}",
            detail=_bounded_decision_preview(lines),
        )

    if event.name == "edit":
        path = arguments.get("path")
        path_text = path if isinstance(path, str) else "unknown path"
        edits = arguments.get("edits")
        edit_items = edits if isinstance(edits, list) else []
        lines = [f"replacements: {len(edit_items)}"]
        for item in edit_items[:2]:
            if not isinstance(item, Mapping):
                continue
            old_text = item.get("oldText")
            new_text = item.get("newText")
            old_line = old_text if isinstance(old_text, str) else ""
            new_line = new_text if isinstance(new_text, str) else ""
            lines.append(f"- {old_line}")
            lines.append(f"+ {new_line}")
        if len(edit_items) > 2:
            lines.append(f"... {len(edit_items) - 2} more replacements")
        return _DecisionContent(
            title="Edit file?",
            meta=f"{path_text}\n{safety} - cwd: {cwd_text}",
            detail=_bounded_decision_preview(lines),
        )

    try:
        serialized = json.dumps(
            arguments,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
    except (TypeError, ValueError):
        serialized = json.dumps(str(arguments), ensure_ascii=False)
    return _DecisionContent(
        title=f"Allow {event.name}?",
        meta=f"{safety} - cwd: {cwd_text}",
        detail=_bounded_decision_preview(serialized.splitlines()),
    )


def _trust_content(event: TrustRequested) -> _DecisionContent:
    return _DecisionContent(
        title="Trust this project?",
        meta=str(event.project_path),
        detail="Trusting allows project-local settings and instructions to load.",
    )


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
    """Deny-first approval/trust selector that temporarily replaces the composer."""

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

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._meta
        yield self._detail
        yield self._options

    @property
    def is_open(self) -> bool:
        return self.display

    def focus_options(self) -> None:
        """Restore keyboard focus to the active deny-first choice list."""

        if self.is_open:
            self._options.focus()

    def show_approval(self, event: ToolApprovalRequested, *, cwd: str) -> None:
        content = _approval_content(event, cwd=cwd)
        self._show(
            content,
            options=[
                Option("Y  Approve once", id="approve_once"),
                Option(f"T  Allow {event.name} for this session", id="tool_session"),
                Option("A  YOLO: allow all tools for this session", id="all_session"),
                Option("N  Deny (default)", id="deny"),
            ],
            default_index=3,
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
                Option("Y  Enable YOLO for this run", id="confirm_all"),
                Option("N  Go back (default)", id="cancel_all"),
            ],
            default_index=1,
            mode="all_confirmation",
        )

    def show_trust(self, event: TrustRequested) -> None:
        self._show(
            _trust_content(event),
            options=[
                Option("Y  Trust project", id="approve"),
                Option("N  Keep untrusted (default)", id="deny"),
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
        key = event.key.lower()
        answer: str | None = None
        if self._mode == "approval":
            answer = {"y": "y", "t": "t", "a": "a", "n": "n", "escape": "n"}.get(key)
        elif self._mode == "all_confirmation":
            answer = {
                "y": "confirm-all",
                "n": "cancel-all",
                "escape": "cancel-all",
            }.get(key)
        elif self._mode == "trust":
            answer = {"y": "y", "n": "n", "escape": "n"}.get(key)
        if answer is None:
            return
        self.submit_answer(answer)
        event.prevent_default()
        event.stop()


class TranscriptEmptyState(Vertical):
    """Centered identity shown only while the transcript has no output."""

    def __init__(self, wordmark: str, hint: str) -> None:
        super().__init__(id="transcript-empty")
        self._wordmark = wordmark
        self._hint = hint

    def compose(self) -> ComposeResult:
        yield Static(
            self._wordmark,
            id="transcript-empty-wordmark",
            markup=False,
        )
        yield Static(
            self._hint,
            id="transcript-empty-hint",
            markup=False,
        )


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
            self._empty_state = TranscriptEmptyState(
                self._empty_wordmark,
                self._empty_hint,
            )
            yield self._empty_state

    def mount_message(self, widget: Widget) -> AwaitMount:
        """Mount output after permanently dismissing the initial empty state."""

        empty_state = self._empty_state
        if empty_state is not None:
            self._empty_state = None
            empty_state.display = False
            empty_state.remove()
        return self.mount(widget)

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
    }
    SlashSuggest > .option-list--option-highlighted {
        background: $accent 30%;
    }
    """

    def __init__(self, id: str | None = None) -> None:  # noqa: A002 - Textual's param name
        super().__init__(id=id)
        # spelling → spec, so the highlighted option's id maps back to its command.
        self._by_command: dict[str, SlashCommandSpec] = {
            spec.command: spec for spec in SLASH_COMMAND_SPECS
        }
        self._visible_specs: tuple[SlashCommandSpec, ...] = ()

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

        return tuple(spec for spec in SLASH_COMMAND_SPECS if spec.command.startswith(query))

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
        self.add_options(
            [Option(f"{spec.command}  {spec.description}", id=spec.command) for spec in specs]
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
# CSS; the role also names the border_title label.
_ROLE_LABELS: dict[str, str] = {
    "user": "you",
    "assistant": "assistant",
    "tool": "tool",
    "approved": "tool",
    "denied": "tool",
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
    _STATUS: dict[str, tuple[str, str]] = {
        "pending": ("⋯", "tool"),
        "denied": ("✗", "denied"),
        "error": ("✗", "denied"),
        "cancelled": ("⊘", "denied"),
        "done": ("✓", "approved"),
    }
    _TICK = 1.0  # the running counter only needs whole-second granularity

    def __init__(self, name: str, arguments: object) -> None:
        super().__init__("")
        self._name = name
        self._summary = _summarize_arguments(arguments)
        self._detail = ""
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

    def set_state(self, status: str, *, detail: str = "", elapsed: float | None = None) -> None:
        """Transition the card to a new status, swapping glyph, color, and detail.

        ``detail`` overrides the argument summary (used to show a denial reason or
        bounded result preview). ``elapsed`` is the true wall-clock duration (from the
        request/result event timestamps); passing it freezes the live counter at
        the honest value and stops the per-card timer. The role CSS class is
        swapped rather than added so the left-rule color reflects only the current
        state.
        """

        glyph, role = self._STATUS.get(status, self._STATUS["pending"])
        self._glyph = glyph
        if detail:
            self._detail = detail
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
        self.border_title = _ROLE_LABELS.get(role, "tool")
        self._repaint()

    def _repaint(self) -> None:
        # name is a fixed tool identifier; summary/detail are escaped as untrusted
        # payload (a path or output the model or a file supplied), preserving
        # the escape-at-boundary invariant the transcript relies on.
        text = f"{self._glyph} [b]{_markup_escape(self._name)}[/b]"
        if not self._detail and self._summary:
            text += f"  [dim]{_markup_escape(self._summary)}[/dim]"
        if self._elapsed is not None:
            text += f" [dim]· {_format_duration(self._elapsed)}[/dim]"
        if self._detail:
            indented = "\n".join(f"  {line}" for line in self._detail.split("\n"))
            text += f"\n[dim]{_markup_escape(indented)}[/dim]"
        self.update(text)


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
