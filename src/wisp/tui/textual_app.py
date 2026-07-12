"""Textual-based fullscreen TUI adapter for Wisp."""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime

import anyio
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Header, TextArea

from wisp.events import (
    AgentCompleted,
    ErrorEvent,
    KnownWispEvent,
    MessageCompleted,
    MessageStarted,
    ProviderRetrying,
    RpcCommandFinished,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolResultReady,
    TrustRequested,
    TurnStarted,
)
from wisp.tui.rendering import (
    TuiRenderer,
    TuiViewSnapshot,
    _markup_escape,
    _truncate_to_cell_width,
    _tui_help_text,
)
from wisp.tui.theme import WISP_THEMES, role_styles
from wisp.tui.widgets import (
    DecisionPanel,
    JumpToLatest,
    LineMessage,
    PromptEditor,
    SlashSuggest,
    StatusBar,
    StreamMessage,
    ToolCard,
    Transcript,
    WorkingIndicator,
    _preview_tool_output,
)

# Plain Rich color names used only before on_mount resolves the themed palette
# (e.g. startup notices written during app construction).
_ROLE_FALLBACK: dict[str, str] = {
    "notice": "cyan",
    "error": "red",
    "dim": "dim",
    "user": "bold magenta",
    "assistant": "bold green",
    "tool": "blue",
    "approved": "green",
    "denied": "red",
}

# The Wisp wordmark, shown while the transcript is empty. Ultra-minimal -
# lowercase, accent color + centering does the work. Quiet over loud.
_WORDMARK = "wisp"
_TAGLINE = "tethered to you"
_EMPTY_TRANSCRIPT_HINT = "Type a prompt or / for commands."

# The input's prompt glyph. The shell hands the Textual renderer a semantic hint
# (`wisp> `, `wisp(running)> `, `approve? [y/N] `) shared with the line/fullscreen
# renderers; the underline-only input reads better with a single terminal-native
# glyph than the verbose `wisp>` chrome, so we swap it in the Textual layer only.
_PROMPT_GLYPH = "❯"

# Semantic-hint → terse Textual placeholder. The footer already spells out the
# mode, so the input line just needs the glyph plus a one-word state cue.
_INPUT_PLACEHOLDERS: dict[str, str] = {
    "wisp> ": f"{_PROMPT_GLYPH} ",
    "wisp(running)> ": f"{_PROMPT_GLYPH} running…",
    "wisp(exiting)> ": f"{_PROMPT_GLYPH} exiting…",
    "approve? [y/N] ": f"{_PROMPT_GLYPH} approve? [y/N]",
}

_RETRY_REASON_LABELS = {
    "network": "network error",
    "timeout": "request timed out",
    "rate_limit": "rate limited",
    "server_error": "server error",
    "transient_http": "temporary HTTP error",
}

# Hard cap on pending large-paste compact echoes (see TextualTui._compact_echoes).
# An echo is registered on Enter but consumed only when the prompt is actually
# echoed; a prompt abandoned before echo (cancelled/quit/errored/empty-dropped
# queued follow-up) would otherwise orphan its entry forever. The cap bounds the
# map so orphans can never accumulate — the oldest is evicted on overflow — and
# each key holds a whole pasted blob, so the bound must stay small.
_MAX_PENDING_ECHOES = 32


def _input_placeholder(hint: str) -> str:
    """Map a shared semantic prompt hint to the Textual input's glyph placeholder.

    Falls back to prefixing the glyph for any hint not in the table (e.g. a future
    mode), so the input always leads with `❯` regardless of the source string.
    """

    return _INPUT_PLACEHOLDERS.get(hint, f"{_PROMPT_GLYPH} {hint}")


def _retry_progress_label(event: ProviderRetrying) -> str:
    """Return compact, human-readable retry progress for the status footer."""

    provider = _truncate_to_cell_width(event.provider, 20)
    reason = _RETRY_REASON_LABELS[event.reason]
    status = f" ({event.status_code})" if event.status_code is not None else ""
    return (
        f"Retrying {provider} · {event.attempt}/{event.max_attempts} "
        f"in {event.delay_seconds:.1f}s · {reason}{status}"
    )


class TextualTui(App[None]):
    """Minimal Textual shell that adapts Wisp's existing TUI loop."""

    # No modal command palette: `/` is the single command affordance (an inline
    # SlashSuggest menu). Disabling this turns off Textual's framework ctrl+p
    # binding at the source — ctrl+p means "previous command" in a terminal, so we
    # don't want it opening a menu.
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen {
        layout: vertical;
    }

    #transcript {
        height: 1fr;
        border: none;
        padding: 0 1;
        scrollbar-size-vertical: 1;
        scrollbar-color: $secondary;
        scrollbar-color-hover: $primary;
        scrollbar-color-active: $accent;
    }

    #transcript-empty {
        width: 1fr;
        height: 1fr;
        min-height: 7;
        align: center middle;
    }

    #transcript-empty-wordmark {
        width: 40;
        max-width: 100%;
        height: 1;
        color: $accent;
        text-align: center;
    }

    #transcript-empty-hint {
        width: 40;
        max-width: 100%;
        height: 1;
        margin-top: 1;
        color: $text-muted;
        text-align: center;
    }

    #jump-latest-row {
        overlay: screen;
        constrain: inside;
        display: none;
        width: 1fr;
        height: 1;
        offset: 0 -1;
        padding: 0 2 0 0;
        align: right middle;
    }

    /* Minimalist messages: a single thin left rule in the role's color carries
       the label; no top border, no fill. Quiet by default, colored only where it
       means something. Colors come only from theme vars present in both themes. */
    .message {
        height: auto;
        margin: 1 0 0 0;
        padding: 0 0 0 1;
        border-left: vkey $secondary;
        border-title-color: $text-muted;
    }

    .message--user {
        border-left: vkey $primary;
    }

    .message--assistant {
        border-left: vkey $success;
    }

    .message--tool,
    .message--notice {
        border-left: vkey $accent;
    }

    .message--approved {
        border-left: vkey $success;
    }

    .message--denied,
    .message--error {
        border-left: vkey $error;
    }

    .message--dim,
    .message--session {
        border-left: none;
        padding-left: 2;
        color: $text-muted;
    }

    #status-bar {
        height: auto;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
        align-vertical: middle;
    }

    #status {
        width: 1fr;
        min-height: 2;
        height: auto;
    }

    /* Underline-only input: no box, just a bottom rule that matches the
       left-rule card language — quiet by default, accent when focused. The
       `❯` prompt glyph (in the placeholder) is the visual anchor, not a frame. */
    #input {
        height: auto;
        max-height: 8;
        border: none;
        border-bottom: heavy $surface-lighten-2;
        padding: 0 1;
        background: $background;
        transition: border 200ms;
    }

    #input:focus {
        border: none;
        border-bottom: heavy $accent;
        background: $surface;
    }
    """

    # priority=True so these fire even while the PromptEditor has focus;
    # otherwise the editor swallows ctrl+d before it reaches the app bindings.
    #
    # Scrollback: the transcript is not in the focused editor's ancestor chain, so
    # its own scroll bindings never fire — forward the keys from the app instead.
    # These are priority bindings because TextArea consumes all four keys. Ctrl+A /
    # Ctrl+E remain available for moving within the prompt.
    #
    # Mouse reporting is enabled in run_shell so wheel and trackpad events reach
    # the transcript. Ctrl+C remains the traditional interrupt; terminal-native
    # selection is still available through the emulator's mouse-bypass modifier.
    BINDINGS = [
        Binding("ctrl+c", "interrupt", "Interrupt", priority=True),
        Binding("ctrl+d", "eof", "EOF", priority=True),
        Binding("pageup", "scroll_transcript_page_up", "Scroll up", priority=True, show=False),
        Binding(
            "pagedown", "scroll_transcript_page_down", "Scroll down", priority=True, show=False
        ),
        Binding("home", "scroll_transcript_home", "Scroll to top", priority=True, show=False),
        Binding("end", "scroll_transcript_end", "Scroll to bottom", priority=True, show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._prompt_send, self._prompt_receive = anyio.create_memory_object_stream[
            str | BaseException
        ](100)
        self._status: StatusBar | None = None
        self._transcript: Transcript | None = None
        self._jump_to_latest: JumpToLatest | None = None
        self._input: PromptEditor | None = None
        self._suggest: SlashSuggest | None = None
        self._decision_panel: DecisionPanel | None = None
        self._current_prompt = "wisp> "
        self._runner: Callable[[], Awaitable[None]] | None = None
        self._runner_error: Exception | None = None
        self._on_submit: Callable[[], None] | None = None
        # Role→Rich-style map for transcript lines, resolved from the active
        # theme and re-derived on theme change (watch_theme). Populated in
        # on_mount. LineMessage widgets carry it as pre-composed markup.
        self._role_styles: dict[str, str] = {}
        # Streaming state: authoritative buffer + the live assistant widget +
        # a coalescing flag so the widget reconciles once per refresh, not once
        # per token (avoids O(n^2) Markdown reparse and the mount race).
        self._streaming_text = ""
        self._stream_widget: StreamMessage | None = None
        self._stream_refresh_pending = False
        # Distinct transcript widgets changed while the user is reading history.
        # A set keeps token deltas and in-place tool-card updates from inflating
        # the jump-to-latest count.
        self._unseen_output: set[Widget] = set()
        # call_id → ToolCard, so the request, approval, and result events for one
        # tool call all mutate the same card instead of stacking three lines.
        self._tool_cards: dict[str, ToolCard] = {}
        # Main-screen heartbeat (opencode-style): a dim WorkingIndicator row in the
        # transcript right after the user prompt, not in the stable footer chrome.
        self._working_indicator: Widget | None = None
        # full submitted text → FIFO of compact echoes (raw editor text with
        # large-paste markers intact). The channel/shell carry only the full str;
        # this side map lets prompt_submitted() echo a compact line without
        # re-plumbing the queue. A per-key FIFO — not a single value — so that
        # submitting the same large paste more than once (e.g. duplicate queued
        # follow-ups) keeps a compact echo for each, consumed in submission order.
        # Entries are registered only when display differs from the full text
        # (i.e. a large paste was expanded) and popped when the echo consumes them.
        # Insertion order is tracked so the map can stay bounded: a prompt
        # abandoned before it echoes (cancel/quit/error/empty-drop) never consumes
        # its entry, so registration evicts the oldest past _MAX_PENDING_ECHOES,
        # and an interrupt/EOF clears the map wholesale.
        self._compact_echoes: dict[str, deque[str]] = {}
        self._echo_order: deque[str] = deque()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            # Transcript takes all remaining height (1fr). The input and compact
            # footer hug the bottom, matching Pi's editor-above-footer visual shape.
            # The input is yielded directly — a wrapping Container would default to
            # height: 1fr and float the input into the middle of the screen.
            yield Transcript(
                empty_wordmark=_WORDMARK,
                empty_hint=_EMPTY_TRANSCRIPT_HINT,
                id="transcript",
            )
            # A full-width transparent overlay row provides right alignment while
            # preserving this natural anchor at the transcript's lower edge.
            with Horizontal(id="jump-latest-row"):
                yield JumpToLatest(id="jump-latest")
            # The slash-command menu floats on the overlay layer anchored near the
            # input; yielded here so it shares the Vertical's coordinate space.
            yield SlashSuggest(id="suggest")
            yield DecisionPanel(id="decision-panel")
            yield PromptEditor(placeholder=_input_placeholder("wisp> "), id="input")
            with Horizontal(id="status-bar"):
                # StatusBar owns the shell snapshot and transient spinner so the
                # same two-line footer surface can reflow safely at compact widths.
                yield StatusBar(id="status")

    async def on_mount(self) -> None:
        # The Header renders these as the wordmark in the top bar: a quiet,
        # lowercase identity that complements the startup splash.
        self.title = "wisp"
        self.sub_title = _TAGLINE
        for theme in WISP_THEMES:
            self.register_theme(theme)
        self.theme = WISP_THEMES[0].name
        self._role_styles = role_styles(self.current_theme)
        self._transcript = self.query_one("#transcript", Transcript)
        self._jump_to_latest = self.query_one("#jump-latest", JumpToLatest)
        self._status = self.query_one("#status", StatusBar)
        self._input = self.query_one("#input", PromptEditor)
        self._suggest = self.query_one("#suggest", SlashSuggest)
        self._decision_panel = self.query_one("#decision-panel", DecisionPanel)
        self._input.focus()  # keep the editor as the resting focus
        if self._runner is not None:
            self.run_worker(self._run_and_exit(), exclusive=True)

    def watch_theme(self, theme_name: str) -> None:
        # `theme` is a Textual reactive; re-derive transcript role colors so
        # message widgets mounted after a switch track the new palette. Already-
        # mounted LineMessage widgets keep their baked-in markup colors.
        if self.is_running:
            self._role_styles = role_styles(self.current_theme)

    async def on_prompt_editor_submitted(self, event: PromptEditor.Submitted) -> None:
        # Enter on a highlighted menu item accepts THAT command (Claude-Code/Codex/
        # Pi model), not the raw buffer — so `/`↓↓ Enter runs the highlighted
        # command even though only `/` was typed. See _accept_menu_highlight_on_enter
        # for the fill-vs-run rules.
        if self._accept_menu_highlight_on_enter(event.value):
            return
        # No live menu: run the line as-is through the typed path.
        if self._suggest is not None:
            self._suggest.hide()
        # Register a compact echo when the submitted (expanded) text differs from
        # what the editor showed — a large paste. The transcript then echoes the
        # marker line, not the whole blob, while the model still gets event.value.
        if event.display != event.value:
            self._register_compact_echo(event.value, event.display)
        self.submit_command_line(event.value)

    def _accept_menu_highlight_on_enter(self, typed: str) -> bool:
        """Accept the highlighted slash command on Enter; return whether it handled.

        Returns False (Enter falls through to submitting the raw line) unless the
        menu is open on a highlighted command.

        The highlight is only *filled and left for editing* when it's still a
        suggestion the user hasn't finished typing — i.e. what they typed is a
        strict prefix of the command (`/` or `/mo` → `/model`) AND the command
        takes an argument, so the trailing space primes the value. Otherwise the
        command is run as typed: an arg-taking command whose name is already fully
        typed (`/model`, `/provider`, `/login`) is a valid bare invocation (show
        current / use defaults), so a single Enter must still run it — filling it
        would silently demand a second Enter. Arg-less commands always run.
        """

        suggest = self._suggest
        if suggest is None or not suggest.is_open:
            return False
        spec = suggest.highlighted_spec()
        if spec is None:
            return False
        suggest.hide()
        # Case-insensitive, matching how the menu matches (query_from_value
        # lowercases): `/MODEL` is a fully-typed `/model`, not a prefix still being
        # typed — otherwise it would fill instead of running. Accepting the highlight
        # always submits the canonical spelling (spec.command), so `/MODEL` runs
        # `/model`, which is what the parser accepts.
        still_typing = typed.lower() != spec.command.lower()  # a prefix like `/`/`/mo`
        if spec.takes_args and still_typing:
            # Prime the value: fill `/cmd ` and let the user type it, then Enter.
            self.prefill_command(f"{spec.command} ")
        else:
            self.submit_command_line(spec.command)
        return True

    def _register_compact_echo(self, prompt: str, display: str) -> None:
        # Append to a per-prompt FIFO so duplicate submissions each keep an echo,
        # tracking global insertion order so the map stays bounded: evict the
        # oldest echo once the total exceeds the cap. An entry orphaned by an
        # abandoned submission (never consumed) is thus reclaimed after enough
        # newer pastes, and can never accumulate without bound.
        self._compact_echoes.setdefault(prompt, deque()).append(display)
        self._echo_order.append(prompt)
        while len(self._echo_order) > _MAX_PENDING_ECHOES:
            oldest = self._echo_order.popleft()
            queued = self._compact_echoes.get(oldest)
            if queued:
                queued.popleft()
                if not queued:
                    del self._compact_echoes[oldest]

    def clear_compact_echoes(self) -> None:
        """Drop all pending compact echoes (the shell dropped its queued prompts).

        Called by the renderer only on paths that actually abandon queued
        follow-ups, so their never-to-be-consumed echoes can't orphan (unbounded
        growth) or be popped by mistake by a later identical paste.
        """

        self._compact_echoes.clear()
        self._echo_order.clear()

    def compact_echo_for(self, prompt: str) -> str:
        """Return the compact transcript echo for a submitted prompt.

        Falls back to the prompt itself when no large-paste echo was registered
        (the common case). Each registered echo is single-use — consumed in
        submission order from the per-prompt FIFO — so N identical large pastes
        each echo compactly, and a later repeat with no fresh paste echoes verbatim.
        """

        echoes = self._compact_echoes.get(prompt)
        if not echoes:
            return prompt
        display = echoes.popleft()
        if not echoes:
            del self._compact_echoes[prompt]
        # Drop the matching insertion-order marker (the oldest for this key) so the
        # cap accounting stays exact and consumed echoes aren't evicted twice.
        with suppress(ValueError):
            self._echo_order.remove(prompt)
        return display

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        # Keep the inline slash menu in sync with the input WITHOUT ever touching
        # the input value — the line is the single source of truth (Claude-Code
        # model), so a leading `/` is always typable as text (`/etc/hosts`). The
        # menu shows only while the value is a bare slash token still being typed;
        # show_for() hides it otherwise (no `/`, a space, or no match).
        if event.text_area is self._input and self._suggest is not None:
            self._suggest.show_for(event.text_area.text)

    def on_transcript_follow_changed(self, event: Transcript.FollowChanged) -> None:
        if event.following:
            self._clear_unseen_output()

    def on_jump_to_latest_selected(self, event: JumpToLatest.Selected) -> None:
        event.stop()
        self.action_scroll_transcript_end()
        if self._input is not None and self._input.display:
            self._input.focus()
        elif self._decision_panel is not None:
            self._decision_panel.focus_options()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._forward_jump_overlay_scroll(event, direction=-1)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._forward_jump_overlay_scroll(event, direction=1)

    def _forward_jump_overlay_scroll(
        self,
        event: events.MouseScrollUp | events.MouseScrollDown,
        *,
        direction: int,
    ) -> None:
        """Route wheel input through the transparent badge-alignment row."""

        jump = self._jump_to_latest
        transcript = self._transcript
        target = event.widget
        if jump is None or transcript is None or target not in {jump, jump.parent}:
            return
        transcript.scroll_to(
            y=transcript.scroll_target_y + direction * self.scroll_sensitivity_y,
            animate=False,
        )
        event.stop()

    def submit_command_line(self, text: str) -> None:
        """Submit a line as if the user typed it and pressed Enter.

        The single entry point for both a real editor submission and a command
        palette selection, so `/command` semantics stay sourced only from the
        shell's typed-line handling. Clears the editor, fires the submit hook (so
        the input mode is captured), then queues the line. send_nowait degrades
        to a notice on a full buffer rather than crashing the action handler.
        """
        if self._input is not None:
            self._input.value = ""
        self._submit_line(text)

    def _submit_decision_line(self, text: str) -> None:
        # The decision overlay temporarily hides the composer. Keep its draft
        # untouched so approval never discards a follow-up the user was typing.
        self._submit_line(text)

    def _submit_line(self, text: str) -> None:
        if self._on_submit is not None:
            self._on_submit()
        try:
            self._prompt_send.send_nowait(text)
        except anyio.WouldBlock:
            self.write_error("input buffer full; command dropped")

    def on_decision_panel_selected(self, event: DecisionPanel.Selected) -> None:
        event.stop()
        self._submit_decision_line(event.answer)

    def prefill_command(self, prefix: str) -> None:
        """Put a command prefix in the editor, cursor at the end, without submitting.

        Tab-completion fills the highlighted command here (`/model `, `/help`); the
        user then adds any argument and presses Enter through the normal typed path.
        """
        if self._input is not None:
            self._input.value = prefix
            self._input.cursor_position = len(prefix)
            self._input.focus()

    async def on_key(self, event: events.Key) -> None:
        # Menu-scoped keys, handled only while the slash menu is open so normal
        # input (Tab focus, Escape, arrows in the editor) is untouched otherwise.
        # Enter is intentionally NOT intercepted — on_input_submitted runs the line.
        suggest = self._suggest
        if suggest is None or not suggest.is_open:
            return
        if event.key == "down":
            suggest.action_cursor_down()
            event.prevent_default()
            event.stop()
        elif event.key == "up":
            suggest.action_cursor_up()
            event.prevent_default()
            event.stop()
        elif event.key == "tab":
            self._complete_from_menu()
            event.prevent_default()
            event.stop()
        elif event.key == "escape":
            # Dismiss but keep whatever the user typed.
            suggest.hide()
            event.prevent_default()
            event.stop()

    def _complete_from_menu(self) -> None:
        # Fill the highlighted command into the input. Arg-taking commands get a
        # trailing space so the user types the value next; arg-less ones don't, so
        # a following Enter runs them immediately. Hiding the menu happens via the
        # resulting on_input_changed (the value gains a space or fully matches).
        suggest = self._suggest
        if suggest is None:
            return
        spec = suggest.highlighted_spec()
        if spec is None:
            return
        self.prefill_command(f"{spec.command} " if spec.takes_args else spec.command)
        suggest.hide()

    def set_submit_hook(self, on_submit: Callable[[], None]) -> None:
        """Register a callback fired the moment an input line is submitted.

        The renderer uses this to snapshot the input mode active at accept time,
        which can differ from the mode observed when `read_prompt()` began
        waiting (e.g. a tool approval arriving mid-line).
        """

        self._on_submit = on_submit

    async def read_prompt(self, prompt: str) -> str:
        self.set_input_hint(prompt)
        value = await self._prompt_receive.receive()
        if isinstance(value, BaseException):
            raise value
        return value

    async def run_shell(self, runner: Callable[[], Awaitable[None]]) -> None:
        self._runner = runner
        # Textual must enable terminal mouse reporting for wheel/trackpad events and
        # scrollbar interaction to reach the Transcript. Keep this explicit: the
        # default is also True, but silently reverting to mouse=False makes the
        # visible scrollbar inert in a real terminal while headless widget tests pass.
        await self.run_async(mouse=True)
        # Textual restores the terminal before returning; re-raise any error
        # from the shell worker here so it surfaces as a normal traceback
        # instead of being swallowed by the app teardown.
        if self._runner_error is not None:
            raise self._runner_error

    async def _run_and_exit(self) -> None:
        if self._runner is None:
            return
        try:
            await self._runner()
        except Exception as exc:
            self._runner_error = exc
        finally:
            self.exit()

    async def close(self) -> None:
        self.exit()

    def copy_to_clipboard(self, text: str) -> None:
        """Copy via pyperclip when available, otherwise Textual's OSC52 fallback."""

        try:
            import pyperclip  # type: ignore[import-untyped]
        except ImportError:
            pass
        else:
            with suppress(Exception):
                pyperclip.copy(text)
        super().copy_to_clipboard(text)

    def _editor_owns_selection(self) -> bool:
        """Whether a selection in the prompt editor owns the current copy gesture.

        The editor only owns copy (ctrl+c or mouse-drag) while it's actually
        visible and focused: when a decision panel is open the composer is hidden
        (display=False) and any stale draft selection must NOT claim ownership, or
        it swallows an interrupt/deny or blocks transcript auto-copy until cleared.
        """

        editor = self._input
        return bool(
            editor is not None and editor.display and editor.has_focus and editor.selected_text
        )

    def _is_streaming(self) -> bool:
        """Whether a streamed assistant turn is mid-flight and mutating the transcript.

        The stream widget is mounted on the first token delta and cleared on
        flush, so its presence (or a non-empty buffer, which the widget lags by a
        frame) marks the window where Textual's selection bounds can go stale.
        """

        return self._stream_widget is not None or bool(self._streaming_text)

    @on(events.TextSelected)
    def on_text_selected(self) -> None:
        """Auto-copy transcript selections so mouse drag + copy works."""

        # Don't auto-copy while the agent is streaming — the transcript
        # mutates and Textual's SELECTED bounds become stale, which would
        # copy unrelated output that scrolled into the selection region.
        # Explicit ctrl+c copy from the prompt editor still works regardless.
        with suppress(Exception):
            if self._transcript is None or self._is_streaming():
                return
            # If the (visible, focused) prompt editor has a selection, it owns the
            # copy — its own ctrl+c / ctrl+insert handling already covers it.
            if self._editor_owns_selection():
                return
            selection = self.screen.get_selected_text()
            if not selection:
                return
            self.copy_to_clipboard(selection)
            self.notify("Copied selection to clipboard.")

    def action_interrupt(self) -> None:
        # If the prompt editor owns the keystroke AND has selected text, ctrl+c
        # means "copy", not "interrupt". Because this binding is priority=True (so
        # it fires before TextArea's own handler and would otherwise swallow copy),
        # we explicitly copy here. Ownership requires the editor to be visible and
        # focused, so a stale selection behind a decision panel can't swallow the
        # interrupt/deny meant for the active approval.
        if self._editor_owns_selection():
            # Copy is best-effort: a broken terminal can make the OSC52 clipboard
            # write (or notify) raise, but the gesture was still a copy, not an
            # interrupt. Return unconditionally so a failed copy never falls
            # through to KeyboardInterrupt (which would also wipe the draft line).
            with suppress(Exception):
                selected = self._input.selected_text  # type: ignore[union-attr]
                self.copy_to_clipboard(selected)
                self.notify(f"Copied {len(selected)} chars to clipboard.")
            return
        self._signal_input(KeyboardInterrupt(), action="interrupt")

    def action_eof(self) -> None:
        self._signal_input(EOFError(), action="EOF")

    # Scrollback: delegate to the Transcript's own scroll actions. Its scroll
    # watcher derives follow intent for normal movement; End uses return_to_latest
    # to restore that intent atomically before jumping. None-guarded like
    # _mount_line for calls before on_mount wires the widget.
    def action_scroll_transcript_page_up(self) -> None:
        if self._transcript is not None:
            self._transcript.action_page_up()

    def action_scroll_transcript_page_down(self) -> None:
        if self._transcript is not None:
            self._transcript.action_page_down()

    def action_scroll_transcript_home(self) -> None:
        if self._transcript is not None:
            self._transcript.action_scroll_home()

    def action_scroll_transcript_end(self) -> None:
        if self._transcript is not None:
            self._transcript.return_to_latest()
        self._clear_unseen_output()

    def _signal_input(self, signal: BaseException, *, action: str) -> None:
        # send_nowait raises WouldBlock if the buffer is full; degrade to a
        # notice rather than crashing the Textual action handler.
        try:
            self._prompt_send.send_nowait(signal)
        except anyio.WouldBlock:
            self.write_error(f"input buffer full; {action} ignored")
            return
        # Drop any partially typed line so it can't be resubmitted on the next
        # Enter after the shell has already handled this interrupt/EOF. Only do
        # this once the signal is actually queued, so a dropped signal doesn't
        # silently discard the user's text without cancelling anything.
        if self._input is not None:
            self._input.value = ""
        # NOTE: pending compact echoes are NOT cleared here. Ctrl+C/EOF is
        # context-dependent shell-side — during an approval/trust prompt it only
        # denies that decision and the queued follow-ups (and their echoes) still
        # run. The shell calls queued_prompts_cleared() on the paths that actually
        # drop the queue, which is where the echoes are reclaimed.

    def set_status(self, snapshot: TuiViewSnapshot) -> None:
        if self._status is not None:
            self._status.set_snapshot(snapshot)

    def status_width(self) -> int | None:
        # The width the footer text actually renders into: the #status widget's
        # content region (padding-excluded), NOT app.size.width. The status bar
        # has horizontal padding, so sizing from the app width over-pads each
        # footer line and makes it wrap/clip past the real region. Falls back to
        # None (no padding) before layout gives the widget a positive width.
        if self._status is None:
            return None
        width = self._status.content_size.width
        return width if width > 0 else None

    def set_input_hint(self, hint: str) -> None:
        self._current_prompt = hint
        if self._input is not None:
            self._input.placeholder = _input_placeholder(hint)

    def show_approval(self, event: ToolApprovalRequested, *, cwd: str) -> None:
        panel = self._decision_panel
        if panel is None:
            return
        if self._suggest is not None:
            self._suggest.hide()
        if self._input is not None:
            self._input.display = False
        panel.show_approval(event, cwd=cwd)

    def show_approval_all_confirmation(self, event: ToolApprovalRequested) -> None:
        panel = self._decision_panel
        if panel is None:
            return
        if self._suggest is not None:
            self._suggest.hide()
        if self._input is not None:
            self._input.display = False
        panel.show_all_confirmation(event)

    def show_trust(self, event: TrustRequested) -> None:
        panel = self._decision_panel
        if panel is None:
            return
        if self._suggest is not None:
            self._suggest.hide()
        if self._input is not None:
            self._input.display = False
        panel.show_trust(event)

    def hide_decision(self) -> None:
        panel = self._decision_panel
        if panel is None or not panel.is_open:
            return
        panel.hide()
        if self._input is not None:
            self._input.display = True
            self._input.focus()

    # --- Main-screen heartbeat (opencode-style) ---

    def _mount_working_indicator(self, indicator: WorkingIndicator) -> None:
        if self._transcript is None:
            return
        self._working_indicator = indicator
        self._transcript.mount_message(indicator)
        # Surface the heartbeat to a scrolled-back reader too: activity no longer
        # lives in the footer, so the jump-to-latest badge is the only cue that
        # working/retry state has begun. No-op while following the tail.
        self._note_transcript_update(indicator)
        self._follow_tail_after_refresh()

    def _remove_working_indicator(self) -> None:
        indicator = self._working_indicator
        if indicator is None:
            return
        self._working_indicator = None
        # The heartbeat is transient — drop it from the unseen set on removal so a
        # retired indicator never inflates the badge or leaves it pointing at a
        # widget no longer in the transcript.
        self._discard_unseen_output(indicator)
        with suppress(Exception):
            indicator.remove()

    def show_working_indicator(self) -> None:
        existing = self._working_indicator
        if isinstance(existing, WorkingIndicator):
            existing.show_working()
            return
        # No active indicator — create a fresh one in the transcript timeline.
        self._remove_working_indicator()
        indicator = WorkingIndicator()
        indicator.restart_working()
        self._mount_working_indicator(indicator)

    def show_retry_indicator(self, label: str) -> None:
        existing = self._working_indicator
        if isinstance(existing, WorkingIndicator):
            existing.show_retry(label)
            return
        indicator = WorkingIndicator()
        indicator.show_retry(label)
        self._mount_working_indicator(indicator)

    def restart_working_indicator(self) -> None:
        """Start fresh transcript activity for a newly submitted prompt."""

        self._remove_working_indicator()
        indicator = WorkingIndicator()
        indicator.restart_working()
        self._mount_working_indicator(indicator)

    def hide_working_indicator(self) -> None:
        self._remove_working_indicator()

    def mount_tool_call(self, call_id: str, name: str, arguments: object) -> None:
        # Mount a fresh card for a tool call and register it by call_id. The
        # status activity is retired: this card now carries the "in progress"
        # signal (pending glyph + dim rule) for the rest of the call's lifecycle.
        if self._transcript is None:
            return
        self.hide_working_indicator()
        card = ToolCard(name, arguments)
        self._tool_cards[call_id] = card
        self._transcript.mount_message(card)
        self._note_transcript_update(card)
        self._follow_tail_after_refresh()

    def resolve_tool_call(
        self, call_id: str, status: str, *, detail: str = "", elapsed: float | None = None
    ) -> None:
        # Transition the card for this call_id in place. If the request card was
        # never seen (a result arriving with no prior request, e.g. after a
        # resume), there is nothing to mutate — drop it rather than mint a
        # half-formed card, keeping the registry the single source of truth.
        # `elapsed` is the true wall-clock duration; it freezes the live counter.
        card = self._tool_cards.get(call_id)
        if card is None:
            return
        card.set_state(status, detail=detail, elapsed=elapsed)
        self._note_transcript_update(card)
        # A terminal state (done/denied/error) ends the call's lifecycle; forget
        # the card so the registry doesn't grow across a long session. The widget
        # stays mounted in the transcript — we just stop tracking it.
        if status != "pending":
            del self._tool_cards[call_id]
        self._follow_tail_after_refresh()

    def fail_pending_tool_calls(self, detail: str = "cancelled") -> None:
        # Drain every still-pending tool card when a turn ends without results —
        # a cancel, a send/shutdown failure, or an RPC stream that dies after
        # ToolCallRequested but before ToolResultReady. Without this the card
        # keeps spinning forever (timer running) while the user continues in the
        # same session. Mark each cancelled (which stops its timer) and clear the
        # registry so nothing leaks into the next turn. The renderer clears its
        # own request-timestamp map alongside this call.
        if not self._tool_cards:
            return
        for card in self._tool_cards.values():
            card.set_state("cancelled", detail=detail)
            self._note_transcript_update(card)
        self._tool_cards.clear()

    def _style(self, role: str) -> str:
        # Resolve a transcript role to a theme-derived Rich style. Falls back to
        # a plain color name if called before on_mount populates the map.
        return self._role_styles.get(role, _ROLE_FALLBACK.get(role, ""))

    def _write_styled(self, role: str, message: str) -> None:
        style = self._style(role)
        escaped = _markup_escape(message)
        self._mount_line(role, f"[{style}]{escaped}[/{style}]" if style else escaped)

    def write_notice(self, message: str) -> None:
        self._write_styled("notice", message)

    def write_error(self, message: str) -> None:
        self._write_styled("error", message)

    def write_dim(self, message: str) -> None:
        self._write_styled("dim", message)

    def write_user(self, message: str) -> None:
        self.write_labeled("you:", message, role="user")

    def write_assistant(self, message: str) -> None:
        self.write_labeled("assistant:", message, role="assistant")

    def write_labeled(self, label: str, message: str = "", *, role: str) -> None:
        # `label` is a fixed literal styled with the role's theme color; `message`
        # is untrusted and escaped, preserving the escape-at-boundary invariant.
        style = self._style(role)
        text = f"[{style}]{label}[/{style}]" if style else label
        if message:
            text += f" {_markup_escape(message)}"
        self._mount_line(role, text)

    def _mount_line(self, role: str, markup: str) -> None:
        # Mount one role-styled LineMessage. Transcript owns the transcript and
        # its own follow-the-tail intent; we just re-assert the follow after the
        # mount lays out.
        if self._transcript is None:
            return
        widget = LineMessage(markup, role=role)
        self._transcript.mount_message(widget)
        self._note_transcript_update(widget)
        self._follow_tail_after_refresh()

    def append_stream(self, delta: str) -> None:
        # Accumulate into the authoritative buffer; lazily mount the streaming
        # assistant widget on the first delta; reconcile via one coalesced
        # refresh so the Markdown reparses at most once per frame, not per token.
        self.hide_working_indicator()
        self._streaming_text += delta
        if self._stream_widget is None and self._transcript is not None:
            self._stream_widget = StreamMessage()
            self._transcript.mount_message(self._stream_widget)
        self._schedule_stream_refresh()

    def flush_stream(self) -> None:
        # Finalize the streamed turn. This is the ONLY place a streamed assistant
        # bubble is completed: the shell suppresses the trailing MessageCompleted
        # when tokens were rendered (shell.py de-dup), so it never reaches event().
        # Capture the widget + final text and reconcile AFTER refresh, because the
        # widget may have been mounted this same tick — reconciling inline would
        # hit the mount race and drop the content.
        self.hide_working_indicator()
        widget = self._stream_widget
        final_text = self._streaming_text
        self._streaming_text = ""
        self._stream_widget = None
        self._stream_refresh_pending = False
        if widget is not None:
            self.call_after_refresh(self._finalize_stream, widget, final_text)

    async def _finalize_stream(self, widget: StreamMessage, text: str) -> None:
        await self._follow_tail_after_content(widget, widget.set_content(text))

    def _schedule_stream_refresh(self) -> None:
        if self._stream_refresh_pending:
            return
        self._stream_refresh_pending = True
        # call_after_refresh runs the reconcile once the pending mount/refresh
        # settles, sidestepping the mount race (update() on a not-yet-mounted
        # widget silently drops content). Textual awaits coroutine callbacks, so
        # the reconcile can await the Markdown mount before following the tail.
        self.call_after_refresh(self._reconcile_stream)

    async def _reconcile_stream(self) -> None:
        self._stream_refresh_pending = False
        if self._stream_widget is not None:
            await self._follow_tail_after_content(
                self._stream_widget, self._stream_widget.set_content(self._streaming_text)
            )

    async def _follow_tail_after_content(
        self,
        widget: StreamMessage,
        await_content: Awaitable[None],
    ) -> None:
        # Await the Markdown update's AwaitComplete so this update's block children
        # have mounted, THEN follow the tail — the scroll lands on the grown extent
        # instead of a partially-mounted one. This replaces guessing a fixed number
        # of refresh cycles with the update's own completion signal. The Transcript
        # still decides whether to scroll (it stays put if the user scrolled away).
        await await_content
        self._note_transcript_update(widget)
        if self._transcript is not None:
            self._transcript.follow_tail()

    def _follow_tail_after_refresh(self) -> None:
        # Non-streamed lines (LineMessage) mount synchronously enough that one
        # post-refresh pass reaches the settled scroll range; used by _mount_line.
        if self._transcript is not None:
            self.call_after_refresh(self._transcript.follow_tail)

    def _note_transcript_update(self, widget: Widget) -> None:
        transcript = self._transcript
        jump = self._jump_to_latest
        if transcript is None or jump is None or transcript.is_following:
            return
        self._unseen_output.add(widget)
        jump.show_count(len(self._unseen_output))

    def _discard_unseen_output(self, widget: Widget) -> None:
        # Forget one widget (e.g. a retired heartbeat) and reconcile the badge:
        # hide it once nothing unseen remains, otherwise shrink the count.
        if widget not in self._unseen_output:
            return
        self._unseen_output.discard(widget)
        jump = self._jump_to_latest
        if jump is None:
            return
        if self._unseen_output:
            jump.show_count(len(self._unseen_output))
        else:
            jump.hide()

    def _clear_unseen_output(self) -> None:
        self._unseen_output.clear()
        if self._jump_to_latest is not None:
            self._jump_to_latest.hide()


class TextualTuiRenderer:
    """Renderer adapter consumed by `TuiShell` and backed by `TextualTui`."""

    def __init__(self, app: TextualTui) -> None:
        self.app = app
        # Mode the shell last reported via view_updated(); this is the mode in
        # effect while the user types the next line.
        self._visible_input_mode = "idle"
        self._visible_cwd = ""
        # Mode captured at the instant a line was submitted. It can differ from
        # the mode the shell polled when read_prompt() began waiting (e.g. a
        # tool approval that arrived mid-line), so the shell reconciles against
        # it via consume_submitted_input_mode().
        self._submitted_input_mode: str | None = None
        app.set_submit_hook(self._capture_submitted_input_mode)
        # call_id → request event timestamp, so a tool card can show the true
        # wall-clock duration (result.timestamp − request.timestamp) when it
        # resolves. Every WispEvent carries a UTC timestamp, so this needs no clock.
        self._tool_started: dict[str, datetime] = {}
        self._progress_active = False
        self._progress_turn: int | None = None
        self._response_started = False
        self._retry_attempt = 0

    def view_updated(self, snapshot: TuiViewSnapshot) -> None:
        self._visible_input_mode = snapshot.input_mode
        self._visible_cwd = snapshot.cwd
        self.app.set_input_hint(snapshot.input_hint)
        self.app.set_status(snapshot)
        if snapshot.input_mode != "running":
            self.app.hide_working_indicator()
        if snapshot.input_mode not in {"running", "approval", "trust"}:
            self._finish_progress()
        if snapshot.input_mode not in {"approval", "trust"}:
            self.app.hide_decision()

    def _begin_progress(self) -> None:
        self._progress_active = True
        self._progress_turn = None
        self._response_started = False
        self._retry_attempt = 0
        self.app.restart_working_indicator()

    def _finish_progress(self) -> None:
        self._progress_active = False
        self._progress_turn = None
        self._response_started = False
        self._retry_attempt = 0
        self.app.hide_working_indicator()

    def _suspend_progress(self) -> None:
        self.app.hide_working_indicator()

    def _turn_started(self, turn: int) -> None:
        if not self._progress_active:
            return
        if self._progress_turn is not None and turn <= self._progress_turn:
            return
        self._progress_turn = turn
        self._response_started = False
        self._retry_attempt = 0
        self.app.show_working_indicator()

    def _provider_retrying(self, event: ProviderRetrying) -> None:
        if not self._progress_active:
            return
        if self._progress_turn is None or event.turn > self._progress_turn:
            self._progress_turn = event.turn
            self._response_started = False
            self._retry_attempt = 0
        elif event.turn < self._progress_turn:
            return
        if self._response_started or event.attempt <= self._retry_attempt:
            return
        self._retry_attempt = event.attempt
        self.app.show_retry_indicator(_retry_progress_label(event))

    def _message_started(self, turn: int) -> None:
        if not self._progress_active:
            return
        if self._progress_turn is not None and turn < self._progress_turn:
            return
        if self._progress_turn == turn and self._response_started:
            return
        self._progress_turn = turn
        self._response_started = True
        self._retry_attempt = 0
        self.app.show_working_indicator()

    def _tool_elapsed(self, call_id: str, finished: datetime) -> float | None:
        # True wall-clock duration for a resolving tool call: result timestamp −
        # request timestamp. Pops the start time so the map doesn't grow and a
        # duplicate result (denial then error result for the same call) doesn't
        # double-count. Returns None when the request was never seen (e.g. resume),
        # leaving the card timer-less rather than showing a bogus duration.
        started = self._tool_started.pop(call_id, None)
        if started is None:
            return None
        return (finished - started).total_seconds()

    def _abort_pending_tools(self, detail: str = "cancelled") -> None:
        # A turn ended without results (cancel / failure / stream death): drain any
        # still-pending tool cards and forget their request timestamps so neither
        # a spinning card nor a stale start time leaks into the next turn.
        self._tool_started.clear()
        self.app.fail_pending_tool_calls(detail)

    def _capture_submitted_input_mode(self) -> None:
        self._submitted_input_mode = self._visible_input_mode

    def consume_submitted_input_mode(self, fallback: str) -> str:
        """Return and clear the mode captured when the last line was accepted."""

        mode = self._submitted_input_mode or fallback
        self._submitted_input_mode = None
        return mode

    def startup(self) -> None:
        # Textual renders identity as a disposable empty state. Keeping startup()
        # as a no-op preserves the shared renderer protocol without putting the
        # wordmark into scrollback; line/fullscreen keep their own startup output.
        pass

    def help(self) -> None:
        self.app.write_notice(_tui_help_text())

    def notice(self, message: str) -> None:
        self.app.write_notice(message)

    def command_error(self, message: str) -> None:
        self.app.write_error(message)

    def prompt_submitted(self, prompt: str) -> None:
        # Echo a compact line for large pastes (marker kept) while the model still
        # received the full expanded text via controller.prompt(prompt).
        self.app.write_user(self.app.compact_echo_for(prompt))

    def queued_prompts_cleared(self) -> None:
        # The shell dropped its queued follow-ups (cancel/quit/input-closed/error),
        # so their pending compact echoes will never be consumed — reclaim them.
        self.app.clear_compact_echoes()

    def running(self) -> None:
        self._begin_progress()

    def queued_follow_up(self, count: int) -> None:
        self.app.write_dim(f"queued follow-up #{count}")

    def running_queued_follow_up(self, count: int) -> None:
        self.app.write_dim(f"running queued follow-up; {count} queued")

    def input_closed_finishing_prompt(self) -> None:
        self.app.write_dim("input closed; finishing current prompt")

    def input_cleared(self) -> None:
        self.app.write_dim("input cleared")

    def cancelling(self, message: str) -> None:
        self._finish_progress()
        self.app.write_notice(message)

    def cancel_already_requested(self) -> None:
        self.app.write_dim("cancel already requested")

    def approval_input_closed(self) -> None:
        self.app.write_notice("Approval input closed; denying tool request.")

    def approval_interrupted(self) -> None:
        self.app.write_notice("Approval interrupted; denying tool request.")

    def quit_requested_denying_approval(self) -> None:
        self.app.write_notice("Quit requested; denying pending tool request.")

    def send_failed(self, action: str, error: object) -> None:
        self._finish_progress()
        self.app.hide_decision()
        self._abort_pending_tools(f"send failed: {action}")
        self.app.write_error(f"failed to send {action}: {error}")

    def shutdown_failed(self, error: object) -> None:
        self._finish_progress()
        self._abort_pending_tools("shutdown failed")
        self.app.write_error(f"shutdown failed: {error}")

    def cancelled(self) -> None:
        self._finish_progress()
        self._abort_pending_tools("cancelled")
        self.app.write_notice("cancelled")

    def token_delta(self, delta: str) -> None:
        # Stream live into the assistant Markdown widget; append_stream buffers
        # and coalesces the reconcile. end_token_stream() finalizes the bubble.
        self._suspend_progress()
        self.app.append_stream(delta)

    def end_token_stream(self) -> None:
        self.app.flush_stream()

    def approval_request(self, event: ToolApprovalRequested) -> None:
        self._suspend_progress()
        self.app.show_approval(event, cwd=self._visible_cwd)

    def approval_all_confirmation(self, event: ToolApprovalRequested) -> None:
        self.app.show_approval_all_confirmation(event)

    def trust_request(self, event: TrustRequested) -> None:
        self._suspend_progress()
        self.app.show_trust(event)

    def event(self, event: KnownWispEvent) -> None:
        # Typed dispatch mirroring LineTuiRenderer.event() so tool calls, tool
        # results, and approvals render as distinct, semantically-styled lines
        # instead of an undifferentiated str(event) repr.
        if isinstance(event, TurnStarted):
            self._turn_started(event.turn)
        elif isinstance(event, ProviderRetrying):
            self._provider_retrying(event)
        elif isinstance(event, MessageStarted):
            self._message_started(event.turn)
        elif isinstance(event, MessageCompleted):
            self._suspend_progress()
            if event.content:
                self.app.write_assistant(event.content)
        elif isinstance(event, ToolCallRequested):
            # Mount the evolving card; approval/result mutate it in place. Record
            # the request time so the card can show its true duration on resolve.
            self._suspend_progress()
            self._tool_started[event.call_id] = event.timestamp
            self.app.mount_tool_call(event.call_id, event.name, event.arguments)
        elif isinstance(event, ToolApprovalResolved):
            # Only a denial changes the card here: an approval leaves it pending
            # until the result lands (the tool still has to run). A denial short-
            # circuits to an error result, but flip the card to "denied" now so the
            # reason shows immediately rather than as a generic error line.
            if not event.approved:
                self.app.resolve_tool_call(
                    event.call_id,
                    "denied",
                    detail=event.reason or "denied",
                    elapsed=self._tool_elapsed(event.call_id, event.timestamp),
                )
        elif isinstance(event, ToolResultReady):
            status = "error" if event.is_error else "done"
            self.app.resolve_tool_call(
                event.call_id,
                status,
                detail=_preview_tool_output(event.output),
                elapsed=self._tool_elapsed(event.call_id, event.timestamp),
            )
        elif isinstance(event, AgentCompleted):
            self._finish_progress()
        elif isinstance(event, ErrorEvent):
            self._finish_progress()
            self.app.write_error(f"error: {event.message}")
        elif isinstance(event, RpcCommandFinished):
            if event.command_type == "prompt":
                self._finish_progress()
            if not event.ok:
                self._suspend_progress()
                self._abort_pending_tools("command failed")
                self.app.write_error(f"command failed: {event.error or event.command_id}")
        # Framing/plumbing events (RpcCommandStarted, a successful RpcCommandFinished,
        # AgentStarted, ToolExecutionStarted/Ended, SessionSaved) are intentionally
        # not rendered. They are session/RPC audit, not conversation — and the active
        # session id already lives in the footer, so a per-turn "session saved:"
        # line is pure redundancy. Dropping them keeps the transcript conversational.

    def rpc_event_reader_failed(self, error: str) -> None:
        self._finish_progress()
        self._abort_pending_tools("event reader failed")
        self.app.write_error(f"RPC event reader failed: {error}")

    def rpc_stream_ended_before_command(self, command_id: str) -> None:
        self._finish_progress()
        self._abort_pending_tools("stream ended")
        self.app.write_error(f"RPC stream ended before command finished: {command_id}")

    def rpc_stream_ended_before_shutdown(self, command_id: str) -> None:
        self._finish_progress()
        self._abort_pending_tools("stream ended")
        self.app.write_error(f"RPC stream ended before shutdown finished: {command_id}")

    def rpc_stream_ended_unexpectedly(self) -> None:
        self._finish_progress()
        self._abort_pending_tools("stream ended")
        self.app.write_error("RPC stream ended unexpectedly")


def create_textual_tui() -> tuple[TextualTui, TuiRenderer]:
    """Create a Textual app and renderer pair for `TuiShell`."""

    app = TextualTui()
    return app, TextualTuiRenderer(app)


__all__ = ["TextualTui", "TextualTuiRenderer", "create_textual_tui"]
