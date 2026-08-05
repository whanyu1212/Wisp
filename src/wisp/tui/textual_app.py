"""Textual widget routing and cross-controller orchestration for Wisp's TUI.

State ownership is deliberately narrow: ``TextualInputController`` owns the
process-local input queue, prompt recall, and compact echoes;
``TextualOverlayController`` owns transient overlay/focus transitions;
``TextualHistoryController`` owns persisted-history retention; and
``TextualTranscriptController`` owns transient live transcript presentation.
``MarkdownStreamController`` remains the owner of native asynchronous Markdown
writes. ``TextualTui`` composes these owners, routes framework events, and owns
Textual-specific mounting and layout restoration; it does not interpret agent,
provider, session, approval, or RPC policy.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager, suppress

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.widget import AwaitMount, Widget
from textual.widgets import Static, TextArea

from wisp.events import (
    RpcSessionSummary,
    SessionStats,
    ToolApprovalRequested,
    TrustRequested,
)
from wisp.providers.catalog import ModelCatalogProviderEntry
from wisp.tui.commands import DEFAULT_TUI_COMMAND_CATALOG, TuiCommandCatalog
from wisp.tui.context_widget import ContextStatusOverlay
from wisp.tui.diff_presentation import DiffPresentation
from wisp.tui.overlay import (
    OverlayKind,
    OverlayOperation,
    TextualOverlayController,
)
from wisp.tui.prompt_history_widget import PromptHistoryPicker
from wisp.tui.rendering import (
    TuiRenderer,
    TuiViewSnapshot,
    _markup_escape,
)
from wisp.tui.stream_buffer import MarkdownStreamController
from wisp.tui.textual_input import TextualInputController
from wisp.tui.textual_renderer import TextualTuiRenderer
from wisp.tui.textual_transcript import TextualTranscriptController
from wisp.tui.theme import WISP_THEMES, role_styles
from wisp.tui.widgets import (
    CommandPalette,
    DecisionPanel,
    JumpToLatest,
    LineMessage,
    ModelPicker,
    OperationIndicator,
    PromptEditor,
    SessionPicker,
    SlashSuggest,
    StatusBar,
    ToolCard,
    Transcript,
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

# The Wisp wordmark, shown while the transcript is empty. Wide-spaced uppercase
# lettering gives the compact badge more presence without relying on terminal-
# dependent ASCII art or unsupported font scaling.
_WORDMARK = "W  I  S  P"
_EMPTY_TRANSCRIPT_TAGLINE = "A coding agent that stays in sync"
_EMPTY_TRANSCRIPT_HINT = "Type a prompt or / for commands."
_SESSION_OPERATION_LABELS: dict[OverlayOperation, str] = {
    OverlayOperation.session_catalog: "Loading sessions…",
    OverlayOperation.session_switch: "Switching session…",
}

# Persistent, low-contrast keybinding reminder below the composer. Only real,
# currently-hidden (show=False) affordances — not aspirational: `/` opens the
# slash-command menu (detected from typed input, not a Binding); Enter/Space
# toggle a focused ToolCard's expand/collapse (ToolCard.BINDINGS); Escape
# returns focus from a card to the input (ToolCard.BINDINGS "leave" action).
# Textual-only chrome — deliberately not folded into format_tui_footer_lines,
# which the line/fullscreen renderers also consume.
_KEYBINDING_HINT = (
    "shift+tab plan/build   ctrl+o actions   ctrl+r history   / commands   enter expand   esc back"
)

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
        min-height: 9;
        align: center middle;
    }

    #transcript-empty-wordmark-frame {
        max-width: 100%;
        height: 3;
    }

    #transcript-empty-wordmark {
        width: 16;
        height: 3;
        padding: 0 2;
        border: heavy $accent;
        background: transparent;
        color: $accent;
        text-style: bold;
        text-align: center;
    }

    #transcript-empty-tagline {
        max-width: 100%;
        height: 1;
        margin-top: 1;
        color: $text;
        text-align: center;
    }

    #transcript-empty-hint {
        max-width: 100%;
        height: 1;
        margin-top: 1;
        color: $text-muted;
        text-align: center;
    }

    #transcript-empty-actions {
        max-width: 100%;
        height: 1;
        margin-top: 1;
        color: $text-muted;
        text-style: dim;
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
        Binding("shift+tab", "toggle_agent_mode", "Plan/build", priority=True, show=False),
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
        self._input_controller = TextualInputController(self)
        self._transcript_controller = TextualTranscriptController(self)
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
        self._context_status: ContextStatusOverlay | None = None
        self._operation_indicator: OperationIndicator | None = None
        self._overlay_controller: TextualOverlayController | None = None
        self._command_catalog = DEFAULT_TUI_COMMAND_CATALOG
        self._agent_mode = "build"
        self._current_prompt = "wisp> "
        self._runner: Callable[[], Awaitable[None]] | None = None
        self._runner_error: Exception | None = None
        self._history_page_request_hook: Callable[[], Awaitable[None]] | None = None
        self._history_latest_request_hook: Callable[[], Awaitable[None]] | None = None
        self._history_window_older_hook: Callable[[], bool] | None = None
        self._history_window_latest_hook: Callable[[], bool] | None = None
        self._live_widget_evicted_hook: Callable[[Widget], None] | None = None
        self._live_history_reload_pending = False
        self._live_history_reload_needed = False
        self._history_marker: Widget | None = None
        self._prepending_history = False
        self._history_prepend_mounts: list[AwaitMount] = []
        self._history_prepend_anchor: (
            tuple[Transcript, Widget | None, float, float, bool, int, int] | None
        ) = None
        self._transcript_navigation_generation = 0
        self._history_render_depth = 0
        self._history_render_batch: AbstractContextManager[None] | None = None
        self._history_render_mounts: list[AwaitMount] = []
        self._last_history_render_mounts: tuple[AwaitMount, ...] = ()
        self._history_layout_generation = 0
        self._transcript_epoch = 0
        # Role→Rich-style map for transcript lines, resolved from the active
        # theme and re-derived on theme change (watch_theme). Populated in
        # on_mount. LineMessage widgets carry it as pre-composed markup.
        self._role_styles: dict[str, str] = {}
        self._stream = MarkdownStreamController(self)

    def clear_prompt_editor(self) -> None:
        """Clear the editor when an input-controller transition requests it."""

        if self._input is not None:
            self._input.value = ""

    def write_input_error(self, message: str) -> None:
        """Render a recoverable input-controller queue error."""

        self.write_error(message)

    def compose(self) -> ComposeResult:
        with Vertical():
            # This full-screen overlay must be the first normal-layout child so
            # `overlay: screen` starts at the screen origin rather than below
            # transcript/composer siblings when it becomes visible.
            yield OperationIndicator(id="operation-indicator")
            yield ContextStatusOverlay(id="context-status")
            # Transcript takes all remaining height (1fr). The input and compact
            # footer hug the bottom, matching Pi's editor-above-footer visual shape.
            # The input is yielded directly — a wrapping Container would default to
            # height: 1fr and float the input into the middle of the screen.
            yield Transcript(
                empty_wordmark=_WORDMARK,
                empty_tagline=_EMPTY_TRANSCRIPT_TAGLINE,
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
        # Retain an application title for terminal metadata without spending a
        # permanent screen row on Textual's Header. The disposable welcome state
        # below is Wisp's only visible identity treatment.
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
        self._context_status = self.query_one("#context-status", ContextStatusOverlay)
        self._operation_indicator = self.query_one("#operation-indicator", OperationIndicator)
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
                OverlayKind.context_status: self._context_status,
                OverlayKind.operation_indicator: self._operation_indicator,
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
            self._input_controller.register_compact_echo(event.value, event.display)
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

    def clear_compact_echoes(self) -> None:
        """Drop echoes for queued prompts the shell actually abandoned."""

        self._input_controller.clear_compact_echoes()

    def compact_echo_for(self, prompt: str) -> str:
        """Return the compact transcript echo for one submitted prompt."""

        return self._input_controller.compact_echo_for(prompt)

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
            show_latest = self._history_window_latest_hook
            if show_latest is not None:
                show_latest()
            self._transcript_controller.clear_unseen_output()
            self._stream.resume_if_deferred()
            self._request_live_history_reload()

    async def on_transcript_need_more_history(self, event: Transcript.NeedMoreHistory) -> None:
        event.stop()
        shift_older = self._history_window_older_hook
        if shift_older is not None and shift_older():
            transcript = self._transcript
            if transcript is not None:
                transcript.history_page_request_failed()
            return
        hook = self._history_page_request_hook
        transcript = self._transcript
        if hook is None or transcript is None or not transcript.has_more_history:
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
        # Capture follow intent before Textual's deferred center-scroll of a tall
        # card can drop it; the controller clears this intent after user scrolling.
        if isinstance(event.widget, ToolCard):
            self._transcript_controller.tool_card_focused(event.widget)

    def on_tool_card_toggled(self, event: ToolCard.Toggled) -> None:
        # A card grew or shrank. Re-pin the tail only when the *newest* card (the
        # transcript's last child) is expanded while the reader was following: that
        # keeps its output in view even though focusing a tall card scrolled the tail
        # off first. Expanding an older card, or one the reader scrolled up to reach,
        # leaves the viewport alone so the freshly revealed content isn't yanked away.
        event.stop()
        self._transcript_controller.tool_card_toggled(event.card)

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
        # A user scroll after focusing a card is a deliberate move away from the tail.
        self._transcript_controller.user_scrolled()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self._wheel_event_targets_transcript(event):
            self._cancel_card_expand_repin()
            assert self._transcript is not None
            self._transcript.stop_following()
        self._forward_jump_overlay_scroll(event, direction=-1)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if self._wheel_event_targets_transcript(event):
            self._cancel_card_expand_repin()
        self._forward_jump_overlay_scroll(event, direction=1)

    def _wheel_event_targets_transcript(
        self,
        event: events.MouseScrollUp | events.MouseScrollDown,
    ) -> bool:
        """Return whether wheel input originated in the transcript or its overlay row."""

        transcript = self._transcript
        target = event.widget
        if transcript is None or target is None:
            return False
        if target is transcript or transcript in target.ancestors:
            return True
        jump = self._jump_to_latest
        return jump is not None and target in {jump, jump.parent}

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
        """Submit a typed/command-palette line through the input controller."""

        self._input_controller.submit_line(text, clear_editor=True)

    def _submit_decision_line(self, text: str) -> bool:
        # The decision overlay temporarily hides the composer. Keep its draft
        # untouched so approval never discards a follow-up the user was typing.
        return self._input_controller.submit_line(text, clear_editor=False)

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

    def on_context_status_overlay_cancelled(self, event: ContextStatusOverlay.Cancelled) -> None:
        event.stop()
        self.hide_context_status()

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

    def set_history_latest_request_hook(
        self,
        hook: Callable[[], Awaitable[None]],
    ) -> None:
        """Register the shell callback used to reload an evicted transcript tail."""

        self._history_latest_request_hook = hook

    def set_submit_hook(self, on_submit: Callable[[], None]) -> None:
        """Register the renderer's at-accept input-mode snapshot callback."""

        self._input_controller.set_submit_hook(on_submit)

    async def read_prompt(self, prompt: str) -> str:
        self.set_input_hint(prompt)
        return await self._input_controller.receive()

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

    def action_toggle_agent_mode(self) -> None:
        """Route the plan/build hotkey through the normal slash-command path."""

        command = "/build" if self._agent_mode == "plan" else "/plan"
        self._input_controller.submit_line(command, clear_editor=False)

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
        picker.show(self._input_controller.prompt_history_entries)

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
            self._transcript.page_up()

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
            self._transcript.page_down()

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
            self._transcript.scroll_to_oldest()
            shift_older = self._history_window_older_hook
            if shift_older is not None and shift_older():
                return
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
        show_latest = self._history_window_latest_hook
        if show_latest is not None:
            show_latest()
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
        self._transcript_controller.clear_unseen_output()

    def _signal_input(self, signal: BaseException, *, action: str) -> None:
        # Pending compact echoes remain intact here. Ctrl+C/EOF can merely deny an
        # approval; the shell clears them only when it actually drops follow-ups.
        self._input_controller.signal(signal, action=action)

    def set_status(self, snapshot: TuiViewSnapshot) -> None:
        self._agent_mode = snapshot.mode
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
        """Retain one shell-accepted prompt for this TUI process only."""

        self._input_controller.record_prompt(prompt)

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

    def show_context_status(self, stats: SessionStats) -> None:
        """Show an explicitly requested visual context report."""

        overlay = self._context_status
        overlays = self._overlay_controller
        if overlay is None or overlays is None:
            return
        # A context response may arrive after another interaction or session
        # operation took ownership. Do not let this informational report displace
        # approvals, pickers, or a guarded session transition.
        if overlays.active_overlay is not None or overlays.active_operation is not None:
            return
        overlays.open(OverlayKind.context_status, preserve_viewport=True)
        overlay.show_stats(stats)

    def hide_context_status(self) -> bool:
        """Dismiss the context report and restore its composer/viewport state."""

        overlays = self._overlay_controller
        if overlays is None:
            return False
        return overlays.close(OverlayKind.context_status)

    def session_catalog_started(self) -> None:
        self._start_session_operation(OverlayOperation.session_catalog)

    def session_catalog_finished(self) -> None:
        self._finish_session_operation(OverlayOperation.session_catalog)

    def session_switch_started(self) -> None:
        self._start_session_operation(OverlayOperation.session_switch)

    def session_switch_finished(self) -> None:
        self._finish_session_operation(OverlayOperation.session_switch)

    def _start_session_operation(self, operation: OverlayOperation) -> None:
        """Show typed session work without giving the indicator lifecycle ownership."""

        overlays = self._overlay_controller
        if overlays is not None:
            overlays.start_operation(operation)
        indicator = self._operation_indicator
        if indicator is not None:
            indicator.show_operation(_SESSION_OPERATION_LABELS[operation])

    def _finish_session_operation(self, operation: OverlayOperation) -> None:
        """Hide only after the active typed session operation completed."""

        overlays = self._overlay_controller
        if overlays is None or not overlays.finish_operation(operation):
            return
        indicator = self._operation_indicator
        if indicator is not None:
            indicator.hide()

    def replace_transcript(self) -> None:
        """Drop the previous session's UI-owned transcript bookkeeping."""

        self._transcript_epoch += 1
        self._history_marker = None
        self._prepending_history = False
        self._live_history_reload_pending = False
        self._live_history_reload_needed = False
        self._history_prepend_mounts.clear()
        self._history_prepend_anchor = None
        self._history_render_depth = 0
        batch = self._history_render_batch
        self._history_render_batch = None
        if batch is not None:
            batch.__exit__(None, None, None)
        self._history_render_mounts.clear()
        self._last_history_render_mounts = ()
        self._history_layout_generation += 1
        self._stream.discard()
        self._transcript_controller.reset()
        self._input_controller.clear_compact_echoes()
        if self._transcript is not None:
            self._transcript.clear_messages()

    def set_history_window_hooks(
        self,
        *,
        shift_older: Callable[[], bool],
        show_latest: Callable[[], bool],
    ) -> None:
        """Install renderer-owned history-window navigation callbacks."""

        self._history_window_older_hook = shift_older
        self._history_window_latest_hook = show_latest

    def request_latest_history(self) -> bool:
        """Schedule a durable latest-page reload requested by history retention."""

        hook = self._history_latest_request_hook
        if hook is None:
            return False
        self.run_worker(
            hook(),
            group="history-latest-reload",
            exit_on_error=False,
        )
        return True

    def history_is_at_top(self) -> bool:
        """Return whether persisted-history navigation is at the transcript top."""

        return self._transcript is not None and self._transcript.scroll_y == 0

    def history_is_following(self) -> bool:
        """Return the current tail-follow intent for retained-history appends."""

        return self._transcript is None or self._transcript.is_following

    # --- Transient live transcript presentation ---

    def transcript_available(self) -> bool:
        """Return whether Textual has mounted the live transcript widget."""

        return self._transcript is not None

    def mount_live_transcript_widget(
        self,
        widget: Widget,
        *,
        before: Widget | None = None,
    ) -> None:
        """Mount one live transcript widget through the app's layout owner."""

        self._mount_transcript_message(widget, before=before)

    def remove_live_transcript_widget(self, widget: Widget) -> None:
        """Remove one live transcript widget without surfacing a stale unmount."""

        with suppress(Exception):
            widget.remove()

    def live_transcript_widget_evicted(self, widget: Widget) -> None:
        """Let the renderer release live de-duplication after bounded eviction."""

        hook = self._live_widget_evicted_hook
        if hook is not None:
            hook(widget)
        self._live_history_reload_needed = True
        self._request_live_history_reload()

    def _request_live_history_reload(self) -> None:
        transcript = self._transcript
        request_latest = self._history_latest_request_hook
        if (
            not self._live_history_reload_needed
            or self._live_history_reload_pending
            or request_latest is None
            or transcript is None
            or not transcript.is_following
        ):
            return
        self._live_history_reload_pending = True
        self.run_worker(
            request_latest(),
            group="history-latest-reload",
            exit_on_error=False,
        )

    def live_history_reloaded(self) -> None:
        """Allow another durable refresh after the current live-eviction reload settles."""

        self._live_history_reload_pending = False
        self._live_history_reload_needed = False

    def live_history_reload_failed(self) -> None:
        """Release a failed request while retaining recovery work for a later retry."""

        self._live_history_reload_pending = False

    def set_live_widget_evicted_hook(self, hook: Callable[[Widget], None]) -> None:
        """Register the renderer-owned durable-history identity release hook."""

        self._live_widget_evicted_hook = hook

    def settle_stream_widget(self, widget: Widget) -> None:
        """Bound a completed native Markdown stream like other settled live output."""

        self._transcript_controller.settle_widget(widget, durable_entry_count=1)

    def transcript_is_following(self) -> bool:
        """Return whether a mounted transcript currently follows its tail."""

        return self._transcript is not None and self._transcript.is_following

    def return_transcript_to_latest(self) -> None:
        """Restore transcript tail-follow after a focused newest card expands."""

        if self._transcript is not None:
            self._transcript.return_to_latest()

    def is_newest_transcript_widget(self, widget: Widget) -> bool:
        """Return whether ``widget`` is the latest mounted transcript message."""

        transcript = self._transcript
        return bool(
            transcript is not None and transcript.children and transcript.children[-1] is widget
        )

    def show_unseen_output(self, count: int) -> None:
        """Show the jump-to-latest affordance for distinct unseen live output."""

        if self._jump_to_latest is not None:
            self._jump_to_latest.show_count(count)

    def hide_unseen_output(self) -> None:
        """Hide the jump-to-latest affordance when no live output is unseen."""

        if self._jump_to_latest is not None:
            self._jump_to_latest.hide()

    def show_working_indicator(self) -> None:
        self._transcript_controller.show_working_indicator()

    def show_retry_indicator(self, label: str) -> None:
        self._transcript_controller.show_retry_indicator(label)

    def restart_working_indicator(self) -> None:
        """Start fresh transcript activity for a newly submitted prompt."""

        self._transcript_controller.restart_working_indicator()

    def hide_working_indicator(self) -> None:
        self._transcript_controller.hide_working_indicator()

    def mount_tool_call(
        self,
        call_id: str,
        name: str,
        arguments: object,
        *,
        historical_card_id: str | None = None,
        historical: bool = False,
        before: Widget | None = None,
    ) -> ToolCard | None:
        """Mount and register one evolving live or retained-history tool card."""

        return self._transcript_controller.mount_tool_call(
            call_id,
            name,
            arguments,
            historical_card_id=historical_card_id,
            historical=historical,
            before=before,
        )

    def enrich_historical_tool_call(
        self,
        card_id: str,
        name: str,
        arguments: object,
        *,
        status: str,
        detail: str | Content | DiffPresentation,
        full_output: str,
        truncated: bool,
    ) -> bool:
        """Apply a paged-in call to its already-mounted historical result card."""

        return self._transcript_controller.enrich_historical_tool_call(
            card_id,
            name,
            arguments,
            status=status,
            detail=detail,
            full_output=full_output,
            truncated=truncated,
        )

    def resolve_tool_call(
        self,
        call_id: str,
        status: str,
        *,
        detail: str | Content | DiffPresentation = "",
        elapsed: float | None = None,
        full_output: str = "",
        truncated: bool = False,
    ) -> ToolCard | None:
        """Resolve a registered tool card in place."""

        return self._transcript_controller.resolve_tool_call(
            call_id,
            status,
            detail=detail,
            elapsed=elapsed,
            full_output=full_output,
            truncated=truncated,
        )

    def fail_pending_tool_calls(self, detail: str = "cancelled") -> None:
        """Drain unresolved live tool cards after a cancelled or failed turn."""

        self._transcript_controller.fail_pending_tool_calls(detail)

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

    def mount_history_marker(self, message: str, *, before: Widget | None) -> None:
        """Mount the durable boundary between the session label and history."""

        style = self._style("dim")
        escaped = _markup_escape(message)
        markup = f"[{style}]{escaped}[/{style}]" if style else escaped
        widget = LineMessage(markup, role="dim")
        transcript = self._transcript
        if before is not None and transcript is not None and before.parent is not transcript:
            # Textual may not have attached a widget mounted in the completed
            # history batch yet. Wait for that batch before using its widget as
            # a relative mount point, rather than appending this marker below
            # the restored transcript.
            self._history_marker = widget
            self.run_worker(
                self._mount_history_marker_after_history_batch(
                    widget,
                    before=before,
                    transcript=transcript,
                    epoch=self._transcript_epoch,
                ),
                group="history-marker",
                exit_on_error=False,
            )
            return
        self._mount_history_marker_widget(widget, before=before)

    async def _mount_history_marker_after_history_batch(
        self,
        widget: LineMessage,
        *,
        before: Widget,
        transcript: Transcript,
        epoch: int,
    ) -> None:
        """Wait for restored history before mounting a deferred session marker."""

        await self.wait_for_history_render()
        self.call_after_refresh(
            self._mount_deferred_history_marker,
            widget,
            before,
            transcript,
            epoch,
        )

    def _mount_deferred_history_marker(
        self,
        widget: LineMessage,
        before: Widget,
        transcript: Transcript,
        epoch: int,
    ) -> None:
        """Mount a session marker once its history boundary is attached."""

        if epoch != self._transcript_epoch or transcript is not self._transcript:
            return
        # A failed or superseded history mount may leave the original boundary
        # detached. The transcript head is still a stable boundary that keeps
        # the marker above all restored entries.
        mount_before = (
            before if before.parent is transcript else next(iter(transcript.children), None)
        )
        self._mount_history_marker_widget(widget, before=mount_before)

    def _mount_history_marker_widget(self, widget: LineMessage, *, before: Widget | None) -> None:
        """Mount and retain one session-history boundary widget."""

        self._mount_transcript_message(widget, before=before)
        self._history_marker = widget

    def write_user(self, message: str) -> LineMessage | None:
        return self.write_labeled("you:", message, role="user")

    def write_assistant(self, message: str) -> LineMessage | None:
        return self.write_labeled("assistant:", message, role="assistant")

    def write_labeled(self, label: str, message: str = "", *, role: str) -> LineMessage | None:
        # `label` is a fixed literal styled with the role's theme color; `message`
        # is untrusted and escaped, preserving the escape-at-boundary invariant.
        style = self._style(role)
        text = f"[{style}]{label}[/{style}]" if style else label
        if message:
            text += f" {_markup_escape(message)}"
        return self._mount_line(role, text)

    def _mount_line(self, role: str, markup: str) -> LineMessage | None:
        # Mount one role-styled LineMessage. Transcript owns the transcript and
        # its own follow-the-tail intent; we just re-assert the follow after the
        # mount lays out.
        if self._transcript is None:
            return None
        widget = LineMessage(markup, role=role)
        self._mount_transcript_message(widget)
        self._transcript_controller.settle_widget(
            widget,
            durable_entry_count=1 if role in {"user", "assistant"} else 0,
        )
        self.note_transcript_update(widget)
        self._follow_tail_after_refresh()
        return widget

    def mount_historical_line(
        self,
        role: str,
        message: str,
        *,
        before: Widget | None = None,
    ) -> LineMessage | None:
        """Mount one retained history line without treating it as new output."""

        if self._transcript is None:
            return None
        label = "you:" if role == "user" else "assistant:"
        style = self._style(role)
        markup = f"[{style}]{label}[/{style}]" if style else label
        if message:
            markup += f" {_markup_escape(message)}"
        widget = LineMessage(markup, role=role)
        self._mount_transcript_message(widget, before=before)
        return widget

    def _mount_transcript_message(self, widget: Widget, *, before: Widget | None = None) -> None:
        transcript = self._transcript
        if transcript is None:
            return
        anchor = self._history_prepend_anchor
        anchor_boundary = (
            anchor[1]
            if (
                self._prepending_history
                and anchor is not None
                and anchor[0] is transcript
                and anchor[1] is not None
                and anchor[1].parent is transcript
            )
            else None
        )
        # Textual queues mounts until the next message-pump cycle. Reconciliation
        # can therefore offer a widget mounted earlier in this batch as the next
        # insertion boundary even though it has no parent yet. Textual rejects
        # such a relative mount, so fall back to the stable prepend anchor.
        mount_before = (
            before if before is not None and before.parent is transcript else anchor_boundary
        )
        mounted = transcript.mount_message(widget, before=mount_before)
        if self._prepending_history:
            self._history_prepend_mounts.append(mounted)
        if self._history_render_depth:
            self._history_render_mounts.append(mounted)

    def history_insertion_boundary(self, history_widgets: set[Widget]) -> Widget | None:
        """Return the first live widget after the managed history window."""

        transcript = self._transcript
        if transcript is None:
            return None
        return next(
            (
                child
                for child in transcript.children
                if child is not self._history_marker and child not in history_widgets
            ),
            None,
        )

    def remove_historical_widget(self, widget: Widget) -> None:
        """Evict one retained widget and its transient live-transcript lookups."""

        self._transcript_controller.forget_widget(widget)
        widget.remove()

    def historical_tool_card(self, card_id: str) -> ToolCard | None:
        """Return a mounted historical card for a page-boundary tool exchange."""

        return self._transcript_controller.historical_tool_card(card_id)

    def set_history_window_available(self, *, has_older: bool) -> None:
        """Expose retained older entries to transcript edge navigation."""

        if self._transcript is not None:
            self._transcript.history_window_available(has_older=has_older)

    def begin_history_render(self) -> None:
        """Track one renderer history batch until all of its widgets mount."""

        if self._history_render_depth == 0:
            self._history_render_mounts.clear()
            batch = self.batch_update()
            batch.__enter__()
            self._history_render_batch = batch
        self._history_render_depth += 1

    def finish_history_render(self) -> None:
        """Publish the latest completed history batch's mount awaitables."""

        self._history_render_depth -= 1
        if self._history_render_depth == 0:
            self._last_history_render_mounts = tuple(self._history_render_mounts)
            self._history_render_mounts.clear()
            batch = self._history_render_batch
            self._history_render_batch = None
            if batch is not None:
                batch.__exit__(None, None, None)

    async def wait_for_history_render(self) -> None:
        """Wait until the latest renderer history batch has mounted its widgets."""

        for mounted in self._last_history_render_mounts:
            await mounted

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

    def stream_widget_for_completed_message(self) -> Widget | None:
        """Return the completed stream widget represented by a suppressed message event."""

        return self._stream.last_completed_widget

    @property
    def last_stream_write_count(self) -> int:
        """Return native Markdown writes used by the latest completed stream turn."""

        return self._stream.last_completed_write_count

    async def wait_for_stream_idle(self) -> None:
        """Wait for scheduled native Markdown streaming work to finish."""

        await self._stream.wait_until_idle()

    def _follow_tail_after_refresh(self) -> None:
        # Non-streamed lines (LineMessage) mount synchronously enough that one
        # post-refresh pass reaches the settled scroll range; used by _mount_line.
        if self._transcript is not None:
            self.call_after_refresh(self._transcript.follow_tail)

    def follow_transcript_tail_after_refresh(self) -> None:
        """Settle a historical render at the tail when it began in follow mode."""

        self._follow_tail_after_refresh()

    @property
    def transcript(self) -> Transcript | None:
        """The transcript scroll view, or None before on_mount wires it.

        Public so collaborators (e.g. the StreamCoalescer) reach it through the
        app's surface rather than a private field, matching TextualTuiRenderer.
        """

        return self._transcript

    def note_transcript_update(self, widget: Widget) -> None:
        """Record a live widget update while preserving history-prepend semantics."""

        if not self._prepending_history:
            self._transcript_controller.note_update(widget)

    def record_live_transcript_update(self, widget: Widget) -> None:
        """Surface live-controller updates through the history-prepend guard."""

        self.note_transcript_update(widget)


def create_textual_tui() -> tuple[TextualTui, TuiRenderer]:
    """Create a Textual app and renderer pair for `TuiShell`."""

    app = TextualTui()
    return app, TextualTuiRenderer(app)


__all__ = ["TextualTui", "TextualTuiRenderer", "create_textual_tui"]
