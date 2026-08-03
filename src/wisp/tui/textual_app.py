"""Textual-based fullscreen TUI adapter for Wisp."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import suppress

import anyio
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.widget import AwaitMount, Widget
from textual.widgets import Header, Static, TextArea

from wisp.events import (
    RpcSessionSummary,
    ToolApprovalRequested,
    TrustRequested,
)
from wisp.providers.catalog import ModelCatalogProviderEntry
from wisp.tui.commands import DEFAULT_TUI_COMMAND_CATALOG, TuiCommandCatalog
from wisp.tui.compact_echo import CompactEchoLog
from wisp.tui.overlay import (
    OverlayKind,
    OverlayOperation,
    TextualOverlayController,
)
from wisp.tui.prompt_history import PromptHistory
from wisp.tui.prompt_history_widget import PromptHistoryPicker
from wisp.tui.rendering import (
    TuiRenderer,
    TuiViewSnapshot,
    _markup_escape,
)
from wisp.tui.stream_buffer import MarkdownStreamController
from wisp.tui.textual_renderer import TextualTuiRenderer
from wisp.tui.theme import WISP_THEMES, role_styles
from wisp.tui.widgets import (
    CommandPalette,
    DecisionPanel,
    JumpToLatest,
    LineMessage,
    ModelPicker,
    PromptEditor,
    SessionPicker,
    SlashSuggest,
    StatusBar,
    ToolCard,
    Transcript,
    WorkingIndicator,
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

# The Wisp wordmark, shown while the transcript is empty. Plain lowercase
# text — an ASCII-art figlet-style treatment (solid Unicode block glyphs)
# was tried twice, with two different fonts, and both rendered with visible
# gaps/misalignment depending on the terminal's font rendering — a
# font-independent limitation, not something a different figlet font fixes.
# Styled bold (see #transcript-empty-wordmark CSS) for more visual weight.
# This is the app's only "wisp" identity treatment beyond the bare Header
# title — no separate tagline; that's what previously duplicated here.
_WORDMARK = "wisp"
_EMPTY_TRANSCRIPT_HINT = "Type a prompt or / for commands."

# Persistent, low-contrast keybinding reminder below the composer. Only real,
# currently-hidden (show=False) affordances — not aspirational: `/` opens the
# slash-command menu (detected from typed input, not a Binding); Enter/Space
# toggle a focused ToolCard's expand/collapse (ToolCard.BINDINGS); Escape
# returns focus from a card to the input (ToolCard.BINDINGS "leave" action).
# Textual-only chrome — deliberately not folded into format_tui_footer_lines,
# which the line/fullscreen renderers also consume.
_KEYBINDING_HINT = "ctrl+o actions   ctrl+r history   / commands   enter expand   esc back"

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


def _input_placeholder(hint: str) -> str:
    """Map a shared semantic prompt hint to the Textual input's glyph placeholder.

    Falls back to prefixing the glyph for any hint not in the table (e.g. a future
    mode), so the input always leads with `❯` regardless of the source string.
    """

    return _INPUT_PLACEHOLDERS.get(hint, f"{_PROMPT_GLYPH} {hint}")


class TextualTui(App[None]):
    """Minimal Textual shell that adapts Wisp's existing TUI loop."""

    # Wisp owns a typed, RPC-backed palette. Keep Textual's framework ctrl+p
    # palette disabled so terminal history remains untouched.
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
        /* Width is also set explicitly in Python (TranscriptEmptyState.compose),
           matching #transcript-empty-hint's width so both land on the same true
           center (see the class docstring for why that can't rely on `align:
           center middle` alone). Bold gives it more visual weight — this is
           the app's one full-identity treatment; the header stays plain. */
        max-width: 100%;
        color: $accent;
        text-style: bold;
        text-align: center;
    }

    #transcript-empty-hint {
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

    .message--tool {
        border-left: vkey $accent;
    }

    .message--notice {
        border-left: vkey $warning;
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

    /* A focused tool card (reached by Tab) is the expand/collapse target; a subtle
       fill marks it without competing with the role-colored left rule. */
    ToolCard:focus {
        background: $boost;
    }

    /* One bordered panel frames the editor and its status line as a single
       card — quiet ($secondary) at rest, accent when the editor has focus,
       matching the focus-driven color the old underline-only input used
       (kept as a CSS variable swap, not a new interaction pattern). */
    #composer {
        height: auto;
        border: round $secondary;
        background: $background;
        transition: border 200ms;
    }

    #composer:focus-within {
        border: round $accent;
        background: $surface;
    }

    #status-bar {
        height: auto;
        padding: 0 1;
        color: $text-muted;
        align-vertical: middle;
        border-top: solid $secondary 40%;
    }

    #status {
        width: 1fr;
        min-height: 2;
        height: auto;
    }

    #keybinding-hint {
        height: 1;
        padding: 0 2;
        color: $text-muted;
        text-align: right;
        text-style: dim;
    }

    #input {
        height: auto;
        max-height: 8;
        border: none;
        padding: 0 1;
        background: transparent;
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
        Binding("ctrl+o", "open_command_palette", "Actions", priority=True),
        Binding("ctrl+r", "open_prompt_history", "History", priority=True),
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
        self._command_palette: CommandPalette | None = None
        self._prompt_history_picker: PromptHistoryPicker | None = None
        self._decision_panel: DecisionPanel | None = None
        self._model_picker: ModelPicker | None = None
        self._session_picker: SessionPicker | None = None
        self._overlay_controller: TextualOverlayController | None = None
        self._command_catalog = DEFAULT_TUI_COMMAND_CATALOG
        self._prompt_history = PromptHistory()
        self._current_prompt = "wisp> "
        self._runner: Callable[[], Awaitable[None]] | None = None
        self._runner_error: Exception | None = None
        self._on_submit: Callable[[], None] | None = None
        self._history_page_request_hook: Callable[[], Awaitable[None]] | None = None
        self._history_marker: Widget | None = None
        self._prepending_history = False
        self._history_prepend_mounts: list[AwaitMount] = []
        self._history_prepend_anchor: (
            tuple[Transcript, Widget | None, float, float, bool, int, int] | None
        ) = None
        self._transcript_navigation_generation = 0
        self._history_render_depth = 0
        self._history_render_mounts: list[AwaitMount] = []
        self._last_history_render_mounts: tuple[AwaitMount, ...] = ()
        self._history_layout_generation = 0
        self._transcript_epoch = 0
        # Role→Rich-style map for transcript lines, resolved from the active
        # theme and re-derived on theme change (watch_theme). Populated in
        # on_mount. LineMessage widgets carry it as pre-composed markup.
        self._role_styles: dict[str, str] = {}
        self._stream = MarkdownStreamController(self)
        # Distinct transcript widgets changed while the user is reading history.
        # A set keeps token deltas and in-place tool-card updates from inflating
        # the jump-to-latest count.
        self._unseen_output: set[Widget] = set()
        # call_id → ToolCard, so the request, approval, and result events for one
        # tool call all mutate the same card instead of stacking three lines.
        self._tool_cards: dict[str, ToolCard] = {}
        self._historical_tool_cards: dict[str, ToolCard] = {}
        # Whether the transcript was following the tail at the moment a ToolCard
        # took focus. Captured then (before Textual's deferred center-scroll of a
        # card taller than the viewport flips follow off) so an explicit keyboard
        # expand can re-pin the tail it would otherwise have scrolled away from,
        # without yanking a reader who had deliberately scrolled up.
        self._card_focus_was_following = False
        # Main-screen heartbeat (opencode-style): a dim WorkingIndicator row in the
        # transcript right after the user prompt, not in the stable footer chrome.
        self._working_indicator: Widget | None = None
        # full submitted text → FIFO of compact echoes (raw editor text with
        # large-paste markers intact). The channel/shell carry only the full str;
        # this side map lets prompt_submitted() echo a compact line without
        # re-plumbing the queue. Registered only when display differs from the full
        # text (a large paste was expanded); consumed by compact_echo_for().
        self._echo_log = CompactEchoLog()

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
            yield CommandPalette(id="command-palette")
            yield PromptHistoryPicker(id="prompt-history")
            yield DecisionPanel(id="decision-panel")
            yield ModelPicker(id="model-picker")
            yield SessionPicker(id="session-picker")
            # #composer frames the editor and status line as one bordered panel
            # (input above, status below, no divider between them) instead of a
            # borderless underline input floating over a separately-backgrounded
            # status bar. height: auto is required here — an unstyled Vertical
            # inside this outer Vertical defaults to 1fr and would float the
            # whole composer into the middle of the screen (see the note this
            # replaces, same landmine, now on the wrapper instead of #input).
            with Vertical(id="composer"):
                yield PromptEditor(placeholder=_input_placeholder("wisp> "), id="input")
                with Horizontal(id="status-bar"):
                    # StatusBar owns the shell snapshot and transient spinner so
                    # the same two-line footer surface can reflow safely at
                    # compact widths.
                    yield StatusBar(id="status")
            yield Static(_KEYBINDING_HINT, id="keybinding-hint", markup=False)

    async def on_mount(self) -> None:
        # Bare lowercase title only — no subtitle. The wordmark (below, via
        # TranscriptEmptyState) is the app's one full-identity treatment,
        # shown once while the transcript is empty; the always-visible header
        # only needs a quiet "wisp" so the two don't compete for attention.
        # A subtitle here also previously round-tripped through Textual's
        # title/subtitle em-dash separator, which was the source of mojibake
        # at narrow widths under some terminal encodings.
        self.title = "wisp"
        for theme in WISP_THEMES:
            self.register_theme(theme)
        self.theme = WISP_THEMES[0].name
        self._role_styles = role_styles(self.current_theme)
        self._transcript = self.query_one("#transcript", Transcript)
        self._jump_to_latest = self.query_one("#jump-latest", JumpToLatest)
        self._status = self.query_one("#status", StatusBar)
        self._input = self.query_one("#input", PromptEditor)
        self._suggest = self.query_one("#suggest", SlashSuggest)
        self._command_palette = self.query_one("#command-palette", CommandPalette)
        self._prompt_history_picker = self.query_one("#prompt-history", PromptHistoryPicker)
        self._decision_panel = self.query_one("#decision-panel", DecisionPanel)
        self._model_picker = self.query_one("#model-picker", ModelPicker)
        self._session_picker = self.query_one("#session-picker", SessionPicker)
        self._overlay_controller = TextualOverlayController(
            composer=self._input,
            suggestion=self._suggest,
            transcript=self._transcript,
            overlays={
                OverlayKind.decision: self._decision_panel,
                OverlayKind.model_picker: self._model_picker,
                OverlayKind.session_picker: self._session_picker,
                OverlayKind.command_palette: self._command_palette,
                OverlayKind.prompt_history: self._prompt_history_picker,
            },
            defer_after_refresh=self._defer_overlay_restore,
        )
        self.set_command_catalog(self._command_catalog)
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
        # command even though only `/` was typed.
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
        menu is open on a highlighted command. Enter executes the highlighted
        command, including when the input is only a prefix (`/mo` -> `/model`).
        Tab remains the completion path for adding arguments and appends a space
        for commands that accept them. Destructive commands may require a fully
        typed name before Enter dispatches them.
        """

        suggest = self._suggest
        if suggest is None or not suggest.is_open:
            return False
        spec = suggest.highlighted_spec()
        if spec is None:
            return False
        suggest.hide()
        is_partial = typed.lower() != spec.command.lower()
        if spec.prefill_on_partial_enter and is_partial:
            self.prefill_command(f"{spec.command} ")
        else:
            self.submit_command_line(spec.command)
        return True

    def _register_compact_echo(self, prompt: str, display: str) -> None:
        self._echo_log.register(prompt, display)

    def clear_compact_echoes(self) -> None:
        """Drop all pending compact echoes (the shell dropped its queued prompts).

        Called by the renderer only on paths that actually abandon queued
        follow-ups, so their never-to-be-consumed echoes can't orphan (unbounded
        growth) or be popped by mistake by a later identical paste.
        """

        self._echo_log.clear()

    def compact_echo_for(self, prompt: str) -> str:
        """Return the compact transcript echo for a submitted prompt.

        Falls back to the prompt itself when no large-paste echo was registered
        (the common case). Each registered echo is single-use — consumed in
        submission order from the per-prompt FIFO — so N identical large pastes
        each echo compactly, and a later repeat with no fresh paste echoes verbatim.
        """

        return self._echo_log.take(prompt)

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
            self._stream.resume_if_deferred()

    async def on_transcript_need_more_history(self, event: Transcript.NeedMoreHistory) -> None:
        event.stop()
        hook = self._history_page_request_hook
        if hook is None:
            transcript = self._transcript
            if transcript is not None:
                transcript.history_page_request_failed()
            return
        await hook()

    def on_jump_to_latest_selected(self, event: JumpToLatest.Selected) -> None:
        event.stop()
        self._scroll_transcript_to_latest()
        if self._input is not None and self._input.display:
            self._input.focus()
        elif self._decision_panel is not None:
            self._decision_panel.focus_options()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        # A ToolCard taller than the viewport is center-scrolled by Textual when it
        # takes focus, which settles the transcript off the bottom and flips follow
        # off before an expand can re-pin. Record the follow intent now — this fires
        # before that deferred scroll — so on_tool_card_toggled can restore it. A
        # deliberate user scroll before the expand clears this (_cancel_card_expand_repin),
        # so the re-pin never yanks a reader who has since left the tail.
        if isinstance(event.widget, ToolCard) and self._transcript is not None:
            self._card_focus_was_following = self._transcript.is_following

    def on_tool_card_toggled(self, event: ToolCard.Toggled) -> None:
        # A card grew or shrank. Re-pin the tail only when the *newest* card (the
        # transcript's last child) is expanded while the reader was following: that
        # keeps its output in view even though focusing a tall card scrolled the tail
        # off first. Expanding an older card, or one the reader scrolled up to reach,
        # leaves the viewport alone so the freshly revealed content isn't yanked away.
        event.stop()
        transcript = self._transcript
        if transcript is None:
            return
        is_newest = bool(transcript.children) and transcript.children[-1] is event.card
        if self._card_focus_was_following and is_newest:
            transcript.return_to_latest()

    def on_tool_card_leave_requested(self, event: ToolCard.LeaveRequested) -> None:
        # Escape on a focused card hands focus back to the resting target: the input,
        # or the decision panel's choice list when a panel has hidden the input (same
        # fallback as on_jump_to_latest_selected), so the reader is never stranded on
        # a card with no way back.
        event.stop()
        if self._input is not None and self._input.display:
            self._input.focus()
        elif self._decision_panel is not None:
            self._decision_panel.focus_options()

    def _cancel_card_expand_repin(self) -> None:
        # A user scroll after focusing a card is a deliberate move away from the tail,
        # so an expand must no longer re-pin (see on_descendant_focus). Called only from
        # the *user* scroll paths — the programmatic focus center-scroll doesn't route
        # through them, so the tall-newest-card re-pin it exists for is unaffected.
        self._card_focus_was_following = False

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._cancel_card_expand_repin()
        self._forward_jump_overlay_scroll(event, direction=-1)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._cancel_card_expand_repin()
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

    def _submit_decision_line(self, text: str) -> bool:
        # The decision overlay temporarily hides the composer. Keep its draft
        # untouched so approval never discards a follow-up the user was typing.
        return self._submit_line(text)

    def _submit_line(self, text: str) -> bool:
        if self._on_submit is not None:
            self._on_submit()
        try:
            self._prompt_send.send_nowait(text)
        except anyio.WouldBlock:
            self.write_error("input buffer full; command dropped")
            return False
        return True

    def on_decision_panel_selected(self, event: DecisionPanel.Selected) -> None:
        event.stop()
        self._submit_decision_line(event.answer)

    def on_model_picker_selected(self, event: ModelPicker.Selected) -> None:
        event.stop()
        self.hide_model_picker()
        self._submit_decision_line(event.answer)

    def on_model_picker_cancelled(self, event: ModelPicker.Cancelled) -> None:
        event.stop()
        self.hide_model_picker()

    def on_session_picker_selected(self, event: SessionPicker.Selected) -> None:
        event.stop()
        # Enter the lifecycle before queueing the typed command: the shell awaits
        # the RPC transport before it repeats session_switch_started(), so this
        # closes the interval where Ctrl-C could otherwise clear the hidden draft.
        self.session_switch_started()
        if not self._submit_decision_line(f"/resume {event.session_id}"):
            self.session_switch_finished()

    def on_session_picker_cancelled(self, event: SessionPicker.Cancelled) -> None:
        event.stop()
        self.hide_session_picker()

    def on_command_palette_selected(self, event: CommandPalette.Selected) -> None:
        event.stop()
        descriptor = event.descriptor
        self.hide_command_palette()
        if any(argument.required for argument in descriptor.arguments):
            self.prefill_command(f"{descriptor.slash_command} ")
            return
        self._submit_decision_line(descriptor.slash_command)

    def on_command_palette_cancelled(self, event: CommandPalette.Cancelled) -> None:
        event.stop()
        self.hide_command_palette()

    def on_prompt_history_picker_selected(self, event: PromptHistoryPicker.Selected) -> None:
        event.stop()
        if not self.hide_prompt_history():
            return
        if self._input is not None:
            self._input.restore_prompt(event.prompt)
            self._input.focus()

    def on_prompt_history_picker_cancelled(self, event: PromptHistoryPicker.Cancelled) -> None:
        event.stop()
        self.hide_prompt_history()

    def set_command_catalog(self, catalog: TuiCommandCatalog) -> None:
        """Apply one executable catalog to both Textual command surfaces."""

        self._command_catalog = catalog
        if self._suggest is not None:
            self._suggest.set_catalog(catalog)
        if self._command_palette is not None:
            self._command_palette.set_catalog(catalog)

    def prefill_command(self, prefix: str) -> None:
        """Put a command prefix in the editor, cursor at the end, without submitting.

        Tab-completion fills the highlighted command here (`/model `, `/help`); the
        user then adds any argument and presses Enter through the normal typed path.
        """
        if self._input is not None:
            self._input.value = prefix
            self._input.cursor_position = len(prefix)
            self._input.focus()

    async def on_event(self, event: events.Event) -> None:
        # App.on_event is the earliest point a Key/Mouse/Paste event passes
        # through before Textual forwards it to whatever currently has focus
        # or is at its screen coordinates — earlier than any widget's own
        # on_key/BINDINGS, and earlier than this app's own on_key below. An
        # event timestamped before the overlay controller's barrier was read by the driver
        # before a decision panel opened (or is opening — the barrier is
        # raised before the composer is hidden or
        # focus moves), so it must never reach a focused/hit-tested widget:
        # dropping it here, rather than in DecisionPanel.on_key or
        # PromptEditor.on_key individually, closes the race for every widget
        # that could still be focused (or at those coordinates) when it
        # arrives, not just the one the caller expected by then.
        #
        # events.MouseEvent (not just Key) is gated as a whole base class,
        # not enumerated per subclass: every mouse variant Textual defines
        # (MouseDown, MouseUp, MouseMove, MouseScrollUp/Down/Left/Right) can
        # reach a widget the same way a stale key can, and Click is
        # synthesized from an already-forwarded MouseUp inside App.on_event's
        # own body (see textual.app.App.on_event) rather than delivered as an
        # independent top-level event, so gating the MouseEvent family here
        # transitively blocks a stale Click too. A stale MouseScrollDown in
        # particular could otherwise scroll the decision panel's highlighted
        # option out of view before a legitimate Enter lands, without ever
        # changing which option is logically selected — the same class of bug
        # this barrier exists to prevent, just via scroll instead of a key.
        # Paste is gated separately (it is not a MouseEvent or Key) because a
        # stale paste could otherwise still reach a focused-but-hidden
        # PromptEditor or an OptionList.
        stale_event_types = (events.Key, events.MouseEvent, events.Paste)
        overlays = self._overlay_controller
        if (
            overlays is not None
            and isinstance(event, stale_event_types)
            and overlays.event_is_stale(event.time)
        ):
            return
        await super().on_event(event)

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

    def set_history_page_request_hook(
        self,
        hook: Callable[[], Awaitable[None]],
    ) -> None:
        """Register the shell callback used when the reader reaches transcript history."""

        self._history_page_request_hook = hook

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
            await self._stream.shutdown()
            self.exit()

    async def close(self) -> None:
        await self._stream.shutdown()
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

        The active stream clears on flush, so selection bounds are stable before
        the final Markdown rendering settles.
        """

        return self._stream.is_streaming

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
        # Session pickers and their sequential RPC reads own Ctrl-C as a
        # presentation cancellation/guard, not an agent interruption.
        overlays = self._overlay_controller
        if overlays is not None and overlays.consume_interrupt():
            return
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

    def action_open_command_palette(self) -> None:
        palette = self._command_palette
        if palette is None:
            return
        if palette.is_open:
            self.hide_command_palette()
            return
        if self._input is None or not self._input.display:
            return
        overlays = self._overlay_controller
        if overlays is None:
            return
        overlays.open(OverlayKind.command_palette, preserve_viewport=True)
        composer = self.query_one("#composer")
        palette.styles.max_height = max(4, composer.region.y)
        palette.show()

    def action_open_prompt_history(self) -> None:
        picker = self._prompt_history_picker
        if picker is None:
            return
        if picker.is_open:
            self.hide_prompt_history()
            return
        if self._input is None or not self._input.display:
            return
        overlays = self._overlay_controller
        if overlays is None:
            return
        overlays.open(OverlayKind.prompt_history, preserve_viewport=True)
        composer = self.query_one("#composer")
        picker.styles.max_height = max(4, composer.region.y)
        picker.show(self._prompt_history.entries)

    # Scrollback: delegate to the Transcript's own scroll actions. Its scroll
    # watcher derives follow intent for normal movement; End uses return_to_latest
    # to restore that intent atomically before jumping. None-guarded like
    # _mount_line for calls before on_mount wires the widget.
    #
    # These are priority bindings (see BINDINGS), so they always win over the
    # focused widget's own PageUp/PageDown/Home/End handling. While a decision
    # panel is open, that would otherwise scroll the transcript out from under
    # the user instead of moving the approval highlight — stranding it on
    # whatever option was last selected (e.g. "Approve once") and turning the
    # next Enter into an unintended approval. Delegate to the panel's own
    # OptionList navigation in that case instead.
    def action_scroll_transcript_page_up(self) -> None:
        if self._decision_panel is not None and self._decision_panel.is_open:
            self._decision_panel.move_highlight_page_up()
            return
        if self._command_palette is not None and self._command_palette.is_open:
            self._command_palette.move_highlight_page_up()
            return
        if self._prompt_history_picker is not None and self._prompt_history_picker.is_open:
            self._prompt_history_picker.move_highlight_page_up()
            return
        if self._session_picker is not None and self._session_picker.is_open:
            self._session_picker.move_highlight_page_up()
            return
        self._cancel_card_expand_repin()
        if self._transcript is not None:
            self._transcript.action_page_up()

    def action_scroll_transcript_page_down(self) -> None:
        if self._decision_panel is not None and self._decision_panel.is_open:
            self._decision_panel.move_highlight_page_down()
            return
        if self._command_palette is not None and self._command_palette.is_open:
            self._command_palette.move_highlight_page_down()
            return
        if self._prompt_history_picker is not None and self._prompt_history_picker.is_open:
            self._prompt_history_picker.move_highlight_page_down()
            return
        if self._session_picker is not None and self._session_picker.is_open:
            self._session_picker.move_highlight_page_down()
            return
        self._cancel_card_expand_repin()
        if self._transcript is not None:
            self._transcript.action_page_down()

    def action_scroll_transcript_home(self) -> None:
        if self._decision_panel is not None and self._decision_panel.is_open:
            self._decision_panel.move_highlight_first()
            return
        if self._command_palette is not None and self._command_palette.is_open:
            self._command_palette.move_highlight_first()
            return
        if self._prompt_history_picker is not None and self._prompt_history_picker.is_open:
            self._prompt_history_picker.move_highlight_first()
            return
        if self._session_picker is not None and self._session_picker.is_open:
            self._session_picker.move_highlight_first()
            return
        self._cancel_card_expand_repin()
        if self._transcript is not None:
            self._transcript_navigation_generation += 1
            self._transcript.scroll_home(animate=False)
            self._transcript.request_history_at_top()

    def action_scroll_transcript_end(self) -> None:
        if self._decision_panel is not None and self._decision_panel.is_open:
            self._decision_panel.move_highlight_last()
            return
        if self._command_palette is not None and self._command_palette.is_open:
            self._command_palette.move_highlight_last()
            return
        if self._prompt_history_picker is not None and self._prompt_history_picker.is_open:
            self._prompt_history_picker.move_highlight_last()
            return
        if self._session_picker is not None and self._session_picker.is_open:
            self._session_picker.move_highlight_last()
            return
        self._scroll_transcript_to_latest()

    def _scroll_transcript_to_latest(self) -> None:
        # The actual "scroll to bottom" behavior, split from
        # action_scroll_transcript_end so the jump-to-latest overlay's click
        # handler (on_jump_to_latest_selected) can call it directly. That
        # overlay is a mouse affordance meaning "get me to the bottom of the
        # transcript" — never a decision-panel-navigation gesture — so it must
        # not be redirected to move the panel highlight just because one
        # happens to be open, unlike a real End keypress.
        if self._transcript is not None:
            self._transcript_navigation_generation += 1
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

    def _defer_overlay_restore(self, callback: Callable[[], None]) -> None:
        self.call_after_refresh(callback)

    def hide_command_palette(self) -> None:
        overlays = self._overlay_controller
        if overlays is not None:
            overlays.close(OverlayKind.command_palette)

    def record_prompt(self, prompt: str) -> None:
        """Retain one exact submitted prompt for this TUI process only."""

        self._prompt_history.record(prompt)

    def show_prompt_history(self) -> None:
        self.action_open_prompt_history()

    def hide_prompt_history(self) -> bool:
        overlays = self._overlay_controller
        if overlays is None:
            return False
        return overlays.close(OverlayKind.prompt_history)

    def show_approval(self, event: ToolApprovalRequested, *, cwd: str) -> None:
        panel = self._decision_panel
        overlays = self._overlay_controller
        if panel is None or overlays is None:
            return
        overlays.open(OverlayKind.decision)
        panel.show_approval(event, cwd=cwd)

    def show_approval_all_confirmation(self, event: ToolApprovalRequested) -> None:
        panel = self._decision_panel
        overlays = self._overlay_controller
        if panel is None or overlays is None:
            return
        overlays.open(OverlayKind.decision)
        panel.show_all_confirmation(event)

    def show_trust(self, event: TrustRequested) -> None:
        panel = self._decision_panel
        overlays = self._overlay_controller
        if panel is None or overlays is None:
            return
        overlays.open(OverlayKind.decision)
        panel.show_trust(event)

    def hide_decision(self) -> None:
        overlays = self._overlay_controller
        if overlays is not None:
            overlays.close(OverlayKind.decision)

    def show_model_picker(
        self,
        entries: tuple[ModelCatalogProviderEntry, ...],
        *,
        current_provider: str,
        current_model: str | None,
        current_effort: str | None,
    ) -> None:
        picker = self._model_picker
        overlays = self._overlay_controller
        if picker is None or overlays is None:
            return
        overlays.open(OverlayKind.model_picker)
        picker.show(
            entries,
            current_provider=current_provider,
            current_model=current_model,
            current_effort=current_effort,
        )

    def hide_model_picker(self) -> None:
        overlays = self._overlay_controller
        if overlays is not None:
            overlays.close(OverlayKind.model_picker)

    def show_session_picker(
        self,
        sessions: tuple[RpcSessionSummary, ...],
        *,
        selected_session_id: str | None,
    ) -> None:
        picker = self._session_picker
        overlays = self._overlay_controller
        if picker is None or overlays is None:
            return
        overlays.open(OverlayKind.session_picker)
        picker.show(sessions, selected_session_id=selected_session_id)

    def hide_session_picker(self, *, restore_input: bool = True) -> None:
        overlays = self._overlay_controller
        if overlays is not None:
            overlays.close(OverlayKind.session_picker, restore_composer=restore_input)

    def session_catalog_started(self) -> None:
        overlays = self._overlay_controller
        if overlays is not None:
            overlays.start_operation(OverlayOperation.session_catalog)

    def session_catalog_finished(self) -> None:
        overlays = self._overlay_controller
        if overlays is not None:
            overlays.finish_operation(OverlayOperation.session_catalog)

    def session_switch_started(self) -> None:
        overlays = self._overlay_controller
        if overlays is not None:
            overlays.start_operation(OverlayOperation.session_switch)

    def session_switch_finished(self) -> None:
        overlays = self._overlay_controller
        if overlays is not None:
            overlays.finish_operation(OverlayOperation.session_switch)

    def replace_transcript(self) -> None:
        """Drop the previous session's UI-owned transcript bookkeeping."""

        self._transcript_epoch += 1
        self._history_marker = None
        self._prepending_history = False
        self._history_prepend_mounts.clear()
        self._history_prepend_anchor = None
        self._history_render_depth = 0
        self._history_render_mounts.clear()
        self._last_history_render_mounts = ()
        self._history_layout_generation += 1
        self.hide_working_indicator()
        self._stream.discard()
        self._tool_cards.clear()
        self._historical_tool_cards.clear()
        self._unseen_output.clear()
        self._card_focus_was_following = False
        self._echo_log.clear()
        self._clear_unseen_output()
        if self._transcript is not None:
            self._transcript.clear_messages()

    # --- Main-screen heartbeat (opencode-style) ---

    def _mount_working_indicator(self, indicator: WorkingIndicator) -> None:
        if self._transcript is None:
            return
        self._working_indicator = indicator
        self._mount_transcript_message(indicator)
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

    def mount_tool_call(
        self,
        call_id: str,
        name: str,
        arguments: object,
        *,
        historical_card_id: str | None = None,
    ) -> None:
        # Mount a fresh card for a tool call and register it by call_id. The
        # status activity is retired: this card now carries the "in progress"
        # signal (pending glyph + dim rule) for the rest of the call's lifecycle.
        if self._transcript is None:
            return
        self.hide_working_indicator()
        card = ToolCard(name, arguments)
        self._tool_cards[call_id] = card
        if historical_card_id is not None:
            self._historical_tool_cards[historical_card_id] = card
        self._mount_transcript_message(card)
        self._note_transcript_update(card)
        self._follow_tail_after_refresh()

    def enrich_historical_tool_call(
        self,
        card_id: str,
        name: str,
        arguments: object,
        *,
        status: str,
        detail: str | Content,
        full_output: str,
        truncated: bool,
    ) -> bool:
        """Apply a paged-in tool call to its already-mounted historical result card."""

        card = self._historical_tool_cards.get(card_id)
        if card is None:
            return False
        card.update_call(name, arguments)
        card.set_state(
            status,
            detail=detail,
            full_output=full_output,
            truncated=truncated,
        )
        self._note_transcript_update(card)
        self._follow_tail_after_refresh()
        return True

    def resolve_tool_call(
        self,
        call_id: str,
        status: str,
        *,
        detail: str | Content = "",
        elapsed: float | None = None,
        full_output: str = "",
        truncated: bool = False,
    ) -> None:
        # Transition the card for this call_id in place. If the request card was
        # never seen (a result arriving with no prior request, e.g. after a
        # resume), there is nothing to mutate — drop it rather than mint a
        # half-formed card, keeping the registry the single source of truth.
        # `elapsed` is the true wall-clock duration; it freezes the live counter.
        # `full_output`/`truncated` let the card expand past the collapsed detail.
        card = self._tool_cards.get(call_id)
        if card is None:
            return
        card.set_state(
            status,
            detail=detail,
            elapsed=elapsed,
            full_output=full_output,
            truncated=truncated,
        )
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
        self._mount_transcript_message(widget)
        self._note_transcript_update(widget)
        self._follow_tail_after_refresh()

    def _mount_transcript_message(self, widget: Widget) -> None:
        transcript = self._transcript
        if transcript is None:
            return
        anchor = self._history_prepend_anchor
        before = (
            anchor[1]
            if (
                self._prepending_history
                and anchor is not None
                and anchor[0] is transcript
                and anchor[1] is not None
            )
            else None
        )
        mounted = transcript.mount_message(widget, before=before)
        if self._prepending_history:
            self._history_prepend_mounts.append(mounted)
        if self._history_render_depth:
            self._history_render_mounts.append(mounted)

    def begin_history_render(self) -> None:
        """Track one renderer history batch until all of its widgets mount."""

        if self._history_render_depth == 0:
            self._history_render_mounts.clear()
        self._history_render_depth += 1

    def finish_history_render(self) -> None:
        """Publish the latest completed history batch's mount awaitables."""

        self._history_render_depth -= 1
        if self._history_render_depth == 0:
            self._last_history_render_mounts = tuple(self._history_render_mounts)
            self._history_render_mounts.clear()

    def history_page_loaded(self, *, has_more: bool) -> None:
        """Record pagination state after the current history batch has laid out."""

        transcript = self._transcript
        if transcript is None:
            return
        transcript.history_page_loaded(has_more=has_more)
        self._history_layout_generation += 1
        if not has_more:
            return
        self.run_worker(
            self._settle_history_page_layout(
                transcript,
                self._last_history_render_mounts,
                self._history_layout_generation,
                self._transcript_epoch,
            ),
            group="history-page-layout",
            exit_on_error=False,
        )

    async def _settle_history_page_layout(
        self,
        transcript: Transcript,
        mounts: tuple[AwaitMount, ...],
        generation: int,
        epoch: int,
    ) -> None:
        for mounted in mounts:
            await mounted
        self.call_after_refresh(
            self._settle_history_page_after_refresh,
            transcript,
            generation,
            epoch,
        )

    def _settle_history_page_after_refresh(
        self,
        transcript: Transcript,
        generation: int,
        epoch: int,
    ) -> None:
        if (
            generation != self._history_layout_generation
            or epoch != self._transcript_epoch
            or transcript is not self._transcript
        ):
            return
        transcript.follow_tail()
        self.call_after_refresh(
            self._request_history_if_still_at_top,
            transcript,
            generation,
            epoch,
        )

    def _request_history_if_still_at_top(
        self,
        transcript: Transcript,
        generation: int,
        epoch: int,
    ) -> None:
        if (
            generation == self._history_layout_generation
            and epoch == self._transcript_epoch
            and transcript is self._transcript
        ):
            transcript.request_history_at_top()

    def mark_history_marker(self) -> None:
        """Keep a resumed-session label above every subsequently prepended page."""

        transcript = self._transcript
        if transcript is not None and transcript.children:
            self._history_marker = transcript.children[-1]

    def begin_history_prepend(self) -> None:
        """Capture the viewport before mounting one older transcript page."""

        transcript = self._transcript
        if transcript is None or not transcript.children:
            return
        first_history_entry = next(
            (child for child in transcript.children if child is not self._history_marker),
            None,
        )
        self._prepending_history = True
        self._history_prepend_mounts.clear()
        self._history_prepend_anchor = (
            transcript,
            first_history_entry,
            transcript.scroll_y,
            first_history_entry.region.y if first_history_entry is not None else 0.0,
            transcript.is_following,
            self._transcript_epoch,
            self._transcript_navigation_generation,
        )

    def finish_history_prepend(self) -> None:
        """Restore the reader's anchor after one older page has mounted."""

        self._prepending_history = False
        anchor = self._history_prepend_anchor
        mounts = tuple(self._history_prepend_mounts)
        self._history_prepend_anchor = None
        self._history_prepend_mounts.clear()
        if anchor is not None:
            self.run_worker(
                self._restore_prepend_viewport_after_mounts(anchor, mounts),
                group="history-prepend",
                exit_on_error=False,
            )

    async def _restore_prepend_viewport_after_mounts(
        self,
        anchor: tuple[Transcript, Widget | None, float, float, bool, int, int],
        mounts: tuple[AwaitMount, ...],
    ) -> None:
        for mounted in mounts:
            await mounted
        self.call_after_refresh(self._restore_prepend_viewport, anchor)

    def _restore_prepend_viewport(
        self,
        anchor: tuple[Transcript, Widget | None, float, float, bool, int, int],
    ) -> None:
        (
            transcript,
            anchor_widget,
            scroll_y,
            anchor_y_before,
            following,
            epoch,
            navigation_generation,
        ) = anchor
        if (
            epoch != self._transcript_epoch
            or navigation_generation != self._transcript_navigation_generation
            or transcript is not self._transcript
            or transcript.is_following != following
            or (not following and transcript.scroll_y != scroll_y)
        ):
            return
        transcript.restore_prepend_viewport(
            scroll_y=scroll_y,
            anchor=anchor_widget,
            anchor_y_before=anchor_y_before,
            following=following,
        )

    def append_stream(self, delta: str) -> None:
        self._stream.append(delta)

    def flush_stream(self) -> None:
        self._stream.flush()

    async def wait_for_stream_idle(self) -> None:
        """Wait for scheduled native Markdown streaming work to finish."""

        await self._stream.wait_until_idle()

    def _follow_tail_after_refresh(self) -> None:
        # Non-streamed lines (LineMessage) mount synchronously enough that one
        # post-refresh pass reaches the settled scroll range; used by _mount_line.
        if self._transcript is not None:
            self.call_after_refresh(self._transcript.follow_tail)

    @property
    def transcript(self) -> Transcript | None:
        """The transcript scroll view, or None before on_mount wires it.

        Public so collaborators (e.g. the StreamCoalescer) reach it through the
        app's surface rather than a private field, matching TextualTuiRenderer.
        """

        return self._transcript

    def note_transcript_update(self, widget: Widget) -> None:
        """Record that ``widget`` changed while the user is reading history.

        Public entry point for collaborators; internal callers use the private
        alias below.
        """

        self._note_transcript_update(widget)

    def _note_transcript_update(self, widget: Widget) -> None:
        if self._prepending_history:
            return
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


def create_textual_tui() -> tuple[TextualTui, TuiRenderer]:
    """Create a Textual app and renderer pair for `TuiShell`."""

    app = TextualTui()
    return app, TextualTuiRenderer(app)


__all__ = ["TextualTui", "TextualTuiRenderer", "create_textual_tui"]
