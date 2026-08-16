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
from dataclasses import dataclass
from pathlib import Path

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.widget import AwaitMount, Widget
from textual.widgets import HelpPanel, KeyPanel, Static, TextArea

from wisp.config import WispConfig
from wisp.events import (
    RpcSessionSummary,
    RpcSkillCatalogSnapshot,
    SessionStats,
    SkillInvoked,
    ToolApprovalRequested,
    TrustRequested,
)
from wisp.providers.catalog import ModelCatalogProviderEntry
from wisp.tools.context import ToolContext
from wisp.tui.commands import (
    DEFAULT_TUI_COMMAND_CATALOG,
    TEXTUAL_LOCAL_COMMAND_DESCRIPTORS,
    TuiCommandCatalog,
)
from wisp.tui.connect_widget import ConnectPanel, ConnectPanelMode
from wisp.tui.connections import ConnectionProviderStatus
from wisp.tui.context_widget import ContextStatusOverlay
from wisp.tui.diff_presentation import DiffPresentation
from wisp.tui.diff_viewer import DiffViewer
from wisp.tui.file_index import FileIndexConfig, collect_paths
from wisp.tui.file_suggest import FileSuggest
from wisp.tui.overlay import (
    OverlayKind,
    OverlayOperation,
    TextualOverlayController,
    TranscriptViewportState,
)
from wisp.tui.prompt_history_widget import PromptHistoryPicker
from wisp.tui.rendering import TuiRenderer, TuiViewSnapshot
from wisp.tui.skills import skill_catalog_text, skill_invocation_text
from wisp.tui.state import TuiCancelRequested, TuiQuitRequested
from wisp.tui.stream_buffer import MarkdownStreamController
from wisp.tui.textual_input import TextualInputController
from wisp.tui.textual_renderer import TextualTuiRenderer
from wisp.tui.textual_transcript import TextualTranscriptController
from wisp.tui.theme import (
    DEFAULT_THEME_NAME,
    PAPER_THEME_NAME,
    WISP_DARK_THEME_NAMES,
    WISP_THEME_BY_NAME,
    WISP_THEME_BY_SLUG,
    WISP_THEME_NAMES,
    WISP_THEMES,
)
from wisp.tui.theme_picker import ThemePicker
from wisp.tui.theme_preference import ThemePreferenceState, load_theme_state, save_theme_state
from wisp.tui.tool_call import ToolActionStatus
from wisp.tui.widgets import (
    ComposerPanel,
    DecisionPanel,
    HistoryNavigation,
    HistoryNavigationIntent,
    JumpToLatest,
    LineMessage,
    ModelPicker,
    OperationIndicator,
    PromptEditor,
    SessionPicker,
    SlashSuggest,
    StatusBar,
    StreamMessage,
    ToolCard,
    Transcript,
)

# The Wisp wordmark, shown while the transcript is empty. Drawn from U+2588 FULL
# BLOCK rather than box-drawing or ASCII art: a single, near-universally
# available glyph whose cell is fully painted, so the letterforms hold their
# shape in any monospace font without depending on how a terminal renders
# partial blocks or line-drawing joins.
#
# Letters were laid out as per-glyph grids and joined column-wise; typing the
# rows by hand produces subtly misaligned stems that stop reading as letters.
# The rendered result is committed rather than generated at import time — there
# is no runtime need to re-derive a constant.
#
# Every row is padded to the full width. `text-align: center` centers each line
# independently, so ragged rows drift relative to one another and the letterforms
# shear apart — the block has to be a true rectangle to hold its shape.
_WORDMARK = """\
█   █  ███  ████  ████
█   █   █   █     █  █
█ █ █   █   ████  ████
██ ██   █      █  █   
█   █  ███  ████  █   """
# Cells occupied by the widest wordmark row, used to size the centered block.
_WORDMARK_WIDTH = 22
# The fallback for viewports too short or too narrow for the drawn mark. Plain
# lowercase rather than letterspaced caps: this substitution is triggered partly
# BY narrowness, and letterspacing would pad the very axis that ran out. It also
# matches how the name is written everywhere else in the UI — the window title
# and the `wisp>` prompt — so the small form reads as the product name rather
# than as a shrunken logo.
_WORDMARK_COMPACT = "wisp"
_EMPTY_TRANSCRIPT_TAGLINE = "A coding agent that stays in sync"
_EMPTY_TRANSCRIPT_HINT = "Type a prompt or / for commands."
_MARKDOWN_VISIBLE_MARKERS = frozenset("`*_[]<>#|~-+\\&@")
_SESSION_OPERATION_LABELS: dict[OverlayOperation, str] = {
    OverlayOperation.session_catalog: "Loading sessions…",
    OverlayOperation.session_switch: "Switching session…",
}

# The input's prompt glyph. The shell hands the Textual renderer a semantic hint
# (`wisp> `, `wisp(running)> `, `approve? [y/N] `) shared with the line/fullscreen
# renderers; Textual turns those states into concise editor guidance behind one
# terminal-native glyph instead of repeating the verbose `wisp>` chrome.
_PROMPT_GLYPH = "❯"

# Semantic-hint → terse Textual placeholder. Command activity remains in the
# persistent transcript heartbeat while the editor explains what submission does.
_INPUT_PLACEHOLDERS: dict[str, str] = {
    "wisp> ": f"{_PROMPT_GLYPH} Ask Wisp anything…",
    "wisp(running)> ": f"{_PROMPT_GLYPH} Add a follow-up…",
    "wisp(exiting)> ": f"{_PROMPT_GLYPH} exiting…",
    "approve? [y/N] ": f"{_PROMPT_GLYPH} approve? [y/N]",
}


def _input_placeholder(hint: str) -> str:
    """Map a shared semantic prompt hint to the Textual input's glyph placeholder.

    Falls back to prefixing the glyph for any hint not in the table (e.g. a future
    mode), so the input always leads with `❯` regardless of the source string.
    """

    return _INPUT_PLACEHOLDERS.get(hint, f"{_PROMPT_GLYPH} {hint}")


@dataclass
class _HistoryPrependAnchor:
    transcript: Transcript
    widget: Widget | None
    scroll_y: float
    widget_y: float
    following: bool
    epoch: int
    navigation_generation: int
    navigation: HistoryNavigation


def _merge_history_navigation(
    current: HistoryNavigation,
    incoming: HistoryNavigation,
) -> HistoryNavigation:
    intent = (
        incoming.intent if current.intent is HistoryNavigationIntent.PRESERVE else current.intent
    )
    return HistoryNavigation(
        intent,
        current.remaining_rows + incoming.remaining_rows,
        incoming.reader_generation,
    )


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
        padding: 0 1 3 1;
        overflow-x: hidden;
        /* Keep native scrolling and overflow state without painting scrollbar
           chrome or reserving transcript width for an invisible gutter. */
        scrollbar-visibility: hidden;
        scrollbar-size-vertical: 0;
    }

    /* No `min-height`: a floor here would keep `size.height` pinned above the
       real viewport on a short terminal, so the panel's own resize breakpoints
       could never observe the smaller sizes they exist to handle — and the
       oversized panel would overflow the transcript and clip the wordmark
       mid-glyph instead of switching to the compact badge. */
    #transcript-empty {
        width: 1fr;
        height: 1fr;
        align: center middle;
    }

    /* No border and no fixed size: the drawn letterforms are the mark, and a
       frame around them would read as chrome competing with the lettering.
       `height: auto` lets the tall wordmark and the one-row compact fallback
       share this rule. */
    #transcript-empty-wordmark {
        max-width: 100%;
        height: auto;
        background: transparent;
        color: $accent;
        text-align: center;
    }

    /* `height: auto` on the text rows, not a fixed `1`: on a viewport narrower
       than the block these lines have to wrap. Pinned to one row they truncated
       mid-word instead ("A coding agent", "Type a prompt or /"), which reads as
       a rendering fault rather than a deliberately compact layout. Wrapping
       costs rows, so the panel's height thresholds are what keep the total
       bounded — see TranscriptEmptyState. */
    #transcript-empty-tagline {
        max-width: 100%;
        height: auto;
        margin-top: 1;
        color: $text;
        text-align: center;
    }

    #transcript-empty-hint {
        max-width: 100%;
        height: auto;
        margin-top: 1;
        color: $text-muted;
        text-align: center;
    }

    #transcript-empty-actions {
        max-width: 100%;
        height: auto;
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

    /* Left rails and lifecycle glyphs keep conversation roles legible without
       color. Authored input keeps a subtle surface; assistant prose and tool rows
       stay open on the transcript background. Colors come only from theme vars
       present in both themes. */
    .message {
        height: auto;
        margin: 1 0 0 0;
        padding: 0 0 0 1;
        border-left: outer $secondary;
        border-title-color: $text-muted;
    }

    .message--user {
        border-left: heavy $primary;
        background: $panel;
        color: $foreground;
        text-style: bold;
        padding-right: 1;
    }

    .message--assistant {
        border-left: outer $success;
        background: transparent;
    }

    .message--tool {
        border-left: outer $accent;
        color: $accent;
        padding-right: 1;
    }

    .message--notice {
        border-left: outer $warning;
        color: $warning;
    }

    .message--approved {
        border-left: outer $success;
        background: $success-muted;
        color: $success;
        padding-right: 1;
    }

    .message--denied {
        border-left: outer $warning;
        background: $warning-muted;
        color: $warning;
        padding-right: 1;
    }

    .message--error {
        border-left: outer $error;
        background: $error-muted;
        color: $error;
        padding-right: 1;
    }

    .message--dim,
    .message--session {
        border-left: none;
        padding-left: 2;
        color: $transcript-muted;
    }

    /* Tool activity is a flat action/result tree rather than another bordered
       message card. Semantic classes still identify lifecycle states; focus adds the
       only rail so keyboard navigation remains visible. */
    ToolCard {
        color: $text-muted;
    }

    ToolCard.message--tool,
    ToolCard.message--approved,
    ToolCard.message--denied,
    ToolCard.message--error {
        border-left: none;
        background: transparent;
        color: $text-muted;
        padding-left: 0;
        padding-right: 0;
    }

    ToolCard:focus {
        outline: none;
        outline-left: heavy $accent;
    }

    /* OpenCode-style composer: a filled writing surface with a single left rail,
       then a detached status strip. The panel owns overlay visibility while the
       editor remains the focus and cursor owner. */
    #composer {
        height: auto;
        border-left: heavy $accent;
        background: $surface;
        padding: 1 2 0 2;
    }

    #input {
        height: auto;
        border: none;
        background: transparent;
        color: $foreground;
        padding: 0;
    }

    #input .text-area--placeholder {
        color: $foreground 60%;
    }

    #composer-meta {
        height: 1;
        margin-top: 1;
        color: $foreground 60%;
    }

    #composer.-compact #composer-meta {
        margin-top: 0;
    }

    #composer.-compact {
        padding-top: 0;
    }

    #status {
        width: 1fr;
        height: 1;
        border-left: heavy $accent;
        padding: 0 2;
        color: $foreground 60%;
        background: $background;
    }

    HelpPanel {
        width: 36%;
        min-width: 30;
        max-width: 60;
    }

    Screen.-compact-help HelpPanel {
        split: bottom;
        width: 100%;
        min-width: 0;
        max-width: 100%;
        height: 50%;
        min-height: 8;
        max-height: 18;
        border-left: none;
        border-top: vkey $foreground 30%;
    }
    """

    # Ctrl+C and Ctrl+D are priority bindings so they fire while PromptEditor has
    # focus. Escape is deliberately non-priority: the nearest focused widget or
    # overlay gets the first chance to dismiss itself.
    #
    # Scrollback: the transcript is not in the focused editor's ancestor chain, so
    # its own scroll bindings never fire — forward the keys from the app instead.
    # These are priority bindings because TextArea consumes all four keys. Ctrl+A /
    # Ctrl+E remain available for moving within the prompt.
    #
    # Mouse reporting is enabled in run_shell so wheel and trackpad events reach
    # the transcript. Ctrl+C uses Wisp's double-press quit flow; terminal-native
    # selection remains available through the emulator's mouse-bypass modifier.
    BINDINGS = [
        Binding(
            "ctrl+g",
            "toggle_contextual_help",
            "Guide",
            priority=True,
            id="wisp.contextual_help",
        ),
        Binding("up", "menu_move(-1)", "Previous suggestion", priority=True, show=False),
        Binding("down", "menu_move(1)", "Next suggestion", priority=True, show=False),
        Binding("tab", "menu_complete", "Complete suggestion", priority=True, show=False),
        Binding("ctrl+r", "open_prompt_history", "History", priority=True),
        Binding("shift+tab", "toggle_agent_mode", "Plan/build", priority=True, show=False),
        Binding("ctrl+t", "toggle_theme", "Light/dark", priority=True, show=False),
        Binding("ctrl+c", "interrupt", "Quit", priority=True),
        Binding("ctrl+d", "eof", "EOF", priority=True),
        Binding("escape", "cancel", "Cancel", priority=False, show=False),
        Binding("pageup", "scroll_transcript_page_up", "Scroll up", priority=True, show=False),
        Binding(
            "pagedown", "scroll_transcript_page_down", "Scroll down", priority=True, show=False
        ),
        Binding("home", "scroll_transcript_home", "Scroll to top", priority=True, show=False),
        Binding("end", "scroll_transcript_end", "Scroll to bottom", priority=True, show=False),
    ]

    def __init__(self, *, protected_paths: tuple[str, ...] | None = None) -> None:
        super().__init__()
        # Textual's native wheel handler reads sensitivity from the app, not the
        # scroll view. One row is the smallest stable terminal scroll increment.
        self.scroll_sensitivity_y = 1.0
        # Register and select a Wisp theme before application CSS is parsed so
        # the transcript's custom muted variable is available on first mount.
        for theme in WISP_THEMES:
            self.register_theme(theme)
        self.theme = DEFAULT_THEME_NAME
        # The parent's already-resolved protected-path policy, threaded down from
        # `run_tui`. It is *not* re-derived here: the resolved policy reflects the
        # `--auth-file` override and the trust decision the parent made before
        # startup, neither of which a fresh `WispConfig.from_env` in this process
        # can reconstruct. `None` means an embedded caller supplied nothing, and
        # `_file_index_context` falls back to resolving on its own.
        self._protected_paths = protected_paths
        # Credential files adopted after startup, when a trusted project's config is
        # applied mid-session. Held apart from the snapshot above so `None` keeps
        # meaning "nothing supplied"; see `set_picker_auth_path`.
        self._adopted_auth_paths: tuple[str, ...] = ()
        self._input_controller = TextualInputController(self)
        self._transcript_controller = TextualTranscriptController(self)
        self._status: StatusBar | None = None
        self._composer: ComposerPanel | None = None
        self._transcript: Transcript | None = None
        self._jump_to_latest: JumpToLatest | None = None
        self._input: PromptEditor | None = None
        self._suggest: SlashSuggest | None = None
        self._file_suggest: FileSuggest | None = None
        self._prompt_history_picker: PromptHistoryPicker | None = None
        self._theme_picker: ThemePicker | None = None
        self._decision_panel: DecisionPanel | None = None
        self._connect_panel: ConnectPanel | None = None
        self._model_picker: ModelPicker | None = None
        self._session_picker: SessionPicker | None = None
        self._context_status: ContextStatusOverlay | None = None
        self._diff_viewer: DiffViewer | None = None
        self._operation_indicator: OperationIndicator | None = None
        self._overlay_controller: TextualOverlayController | None = None
        self._help_viewport_state: TranscriptViewportState | None = None
        self._help_viewport_baseline: TranscriptViewportState | None = None
        self._command_catalog = DEFAULT_TUI_COMMAND_CATALOG
        self._last_dark_theme = DEFAULT_THEME_NAME
        self._theme_picker_original: str | None = None
        self._pending_theme_preview: str | None = None
        self._theme_preview_scheduled = False
        self._theme_preview_epoch = 0
        self._skill_catalog = RpcSkillCatalogSnapshot()
        self._agent_mode = "build"
        self._current_prompt = "wisp> "
        self._runner: Callable[[], Awaitable[None]] | None = None
        self._runner_error: Exception | None = None
        self._history_page_request_hook: Callable[[], Awaitable[None]] | None = None
        self._history_latest_request_hook: Callable[[], Awaitable[None]] | None = None
        self._connect_api_key_hook: Callable[[str, str], Awaitable[None]] | None = None
        self._connect_oauth_hook: Callable[[str], Awaitable[None]] | None = None
        self._history_window_older_hook: Callable[[], bool] | None = None
        self._history_window_oldest_hook: Callable[[], bool] | None = None
        self._history_window_latest_hook: Callable[[], bool] | None = None
        self._live_widget_evicted_hook: Callable[[Widget], None] | None = None
        self._live_history_reload_pending = False
        self._live_history_reload_needed = False
        self._live_history_eviction_generation = 0
        self._live_history_reload_generation: int | None = None
        self._live_history_recovery_navigation: HistoryNavigation | None = None
        self._live_history_recovery_blocked = False
        self._history_marker: Widget | None = None
        self._prepending_history = False
        self._history_prepend_mounts: list[AwaitMount] = []
        self._history_prepend_anchor: _HistoryPrependAnchor | None = None
        self._transcript_navigation_generation = 0
        self._pending_history_navigation = HistoryNavigation()
        self._oldest_navigation_generation: int | None = None
        self._history_render_depth = 0
        self._history_render_batch: AbstractContextManager[None] | None = None
        self._history_render_mounts: list[AwaitMount] = []
        self._last_history_render_mounts: tuple[AwaitMount, ...] = ()
        self._history_layout_generation = 0
        self._transcript_epoch = 0
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
            yield DiffViewer(id="diff-viewer")
            # Transcript takes all remaining height (1fr). ComposerPanel is explicitly
            # auto-height so the input and detached footer still hug the screen bottom.
            yield Transcript(
                empty_wordmark=_WORDMARK,
                empty_compact_wordmark=_WORDMARK_COMPACT,
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
            # Same anchored slot as the slash menu; only one is ever open at a time
            # (the `/` and `@` triggers are mutually exclusive by construction).
            yield FileSuggest(id="file-suggest")
            yield PromptHistoryPicker(id="prompt-history")
            yield ThemePicker(id="theme-picker")
            yield DecisionPanel(id="decision-panel")
            yield ConnectPanel(id="connect-panel")
            yield ModelPicker(id="model-picker")
            yield SessionPicker(id="session-picker")
            yield ComposerPanel(placeholder=_input_placeholder("wisp> "), id="composer")
            # Textual uses a distinct one-row information hierarchy. The shared
            # Rich/prompt-toolkit renderers retain their existing two-line footer.
            yield StatusBar(id="status")

    async def on_mount(self) -> None:
        # Retain an application title for terminal metadata without spending a
        # permanent screen row on Textual's Header. The disposable welcome state
        # below is Wisp's only visible identity treatment.
        self.title = "wisp"
        # A persisted choice only selects among Wisp's own themes: Textual also
        # registers ~20 built-ins, and silently adopting one of those from a
        # stale or hand-edited file would leave the transcript's role colors
        # (resolved from the active theme) unrecognizable.
        preference = load_theme_state(
            valid_themes=WISP_THEME_NAMES,
            valid_dark_themes=WISP_DARK_THEME_NAMES,
        )
        self.theme = preference.active_theme or DEFAULT_THEME_NAME
        self._last_dark_theme = preference.last_dark_theme or (
            self.theme if self.theme in WISP_DARK_THEME_NAMES else DEFAULT_THEME_NAME
        )
        self._transcript = self.query_one("#transcript", Transcript)
        self._jump_to_latest = self.query_one("#jump-latest", JumpToLatest)
        self._status = self.query_one("#status", StatusBar)
        self._composer = self.query_one("#composer", ComposerPanel)
        self._input = self.query_one("#input", PromptEditor)
        self._suggest = self.query_one("#suggest", SlashSuggest)
        self._file_suggest = self.query_one("#file-suggest", FileSuggest)
        self._prompt_history_picker = self.query_one("#prompt-history", PromptHistoryPicker)
        self._theme_picker = self.query_one("#theme-picker", ThemePicker)
        self._decision_panel = self.query_one("#decision-panel", DecisionPanel)
        self._connect_panel = self.query_one("#connect-panel", ConnectPanel)
        self._model_picker = self.query_one("#model-picker", ModelPicker)
        self._session_picker = self.query_one("#session-picker", SessionPicker)
        self._context_status = self.query_one("#context-status", ContextStatusOverlay)
        self._diff_viewer = self.query_one("#diff-viewer", DiffViewer)
        self._operation_indicator = self.query_one("#operation-indicator", OperationIndicator)
        self._overlay_controller = TextualOverlayController(
            composer=self._composer,
            # Both composer-anchored menus, so an overlay opening tears down each
            # of them; the `@` picker would otherwise float over the overlay and
            # win the Escape/navigation keys that belong to the active workflow.
            suggestions=(self._suggest, self._file_suggest),
            transcript=self._transcript,
            overlays={
                OverlayKind.decision: self._decision_panel,
                OverlayKind.connect: self._connect_panel,
                OverlayKind.model_picker: self._model_picker,
                OverlayKind.session_picker: self._session_picker,
                OverlayKind.prompt_history: self._prompt_history_picker,
                OverlayKind.theme_picker: self._theme_picker,
                OverlayKind.context_status: self._context_status,
                OverlayKind.diff_viewer: self._diff_viewer,
                OverlayKind.operation_indicator: self._operation_indicator,
            },
            defer_after_refresh=self._defer_overlay_restore,
            on_overlay_displaced=self._on_overlay_displaced,
        )
        self.set_command_catalog(self._command_catalog)
        self.set_skill_catalog(self._skill_catalog)
        self._input.focus()  # keep the editor as the resting focus
        if self._runner is not None:
            self.run_worker(self._run_and_exit(), exclusive=True)

    def on_resize(self, event: events.Resize) -> None:
        """Update responsive help and composer presentation after terminal resize."""

        self.screen.set_class(event.size.width < 80, "-compact-help")
        if self._composer is not None:
            self._composer.refresh_layout(height=event.size.height)

    def watch_theme(self, theme_name: str) -> None:
        # Theme variables recolor mounted CSS-owned content atomically. Widgets
        # with cached theme-derived Rich content refresh their own presentation.
        if self.is_running:
            if self._status is not None:
                self._status.refresh_theme()
            if self._composer is not None:
                self._composer.refresh_theme()

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

        # An open file menu claims Enter first: the user is picking a path, not
        # submitting the prompt. Completion leaves the line intact for further
        # typing, so this always swallows the keypress.
        if self._file_suggest is not None and self._file_suggest.is_open:
            if self._complete_path_from_menu():
                return True

        suggest = self._suggest
        if suggest is None or not suggest.is_open:
            return False
        spec = suggest.highlighted_spec()
        if spec is None:
            return False
        suggest.hide()
        self.refresh_bindings()
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
        # Keep the inline menus in sync with the input WITHOUT ever touching the
        # input value — the line is the single source of truth (Claude-Code model),
        # so a leading `/` is always typable as text (`/etc/hosts`).
        #
        # The two menus are mutually exclusive: a slash command owns the whole line
        # (anchored at position 0), while an `@` mention is cursor-relative and can
        # appear anywhere. Consulting the slash menu first preserves its existing
        # behavior exactly; the file menu is offered only when no slash menu is live.
        if event.text_area is not self._input:
            return
        menu_was_open = self._suggestion_menu_is_open()
        slash_matches = 0
        if self._suggest is not None:
            slash_matches = self._suggest.show_for(event.text_area.text)
        if self._file_suggest is None:
            if menu_was_open != self._suggestion_menu_is_open():
                self.refresh_bindings()
            return
        if slash_matches:
            self._file_suggest.hide()
            if menu_was_open != self._suggestion_menu_is_open():
                self.refresh_bindings()
            return
        self._file_suggest.show_for(
            event.text_area.text, self._file_offset_of_cursor(event.text_area)
        )
        if menu_was_open != self._suggestion_menu_is_open():
            self.refresh_bindings()

    @staticmethod
    def _file_offset_of_cursor(editor: TextArea) -> int:
        """Flat character offset of the caret, for the `@` trigger scan.

        TextArea reports the cursor as (row, column); the mention scan needs a flat
        index into the same string it is handed. PromptEditor exposes exactly this
        conversion as its Input-compatibility shim, so reuse it rather than
        recomputing the line offsets here.
        """

        cursor_position = getattr(editor, "cursor_position", None)
        if isinstance(cursor_position, int):
            return cursor_position
        return len(editor.text)

    def set_picker_auth_path(self, auth_path: Path) -> None:
        """Protect a credential file the session adopted after startup.

        A trusted project's config can move ``auth_path`` mid-session (deferred
        trust), after ``__init__`` captured the startup policy. These paths are kept
        *beside* that snapshot rather than merged into it, so the `None` case still
        means "no policy supplied" and embedded callers keep their fallback
        resolution instead of collapsing to just this one pattern.

        Accumulating rather than replacing is deliberate: a previously active
        credential file must stay hidden, since a config change should never widen
        what the picker is willing to offer.
        """

        pattern = auth_path.expanduser().resolve(strict=False).as_posix()
        if pattern not in self._adopted_auth_paths:
            self._adopted_auth_paths = (*self._adopted_auth_paths, pattern)

    def load_file_suggestions(self, cwd: str) -> None:
        """Collect the `@`-picker corpus for `cwd`, off the event loop.

        Walking a real project costs hundreds of milliseconds of `os.scandir`, which
        would visibly freeze every keystroke and animation if run inline. The walk is
        syscall-bound, so a thread gives genuine concurrency here.
        """

        picker = self._file_suggest
        if picker is None:
            return
        self._collect_file_suggestions(cwd, picker)

    @work(thread=True, exclusive=True, group="file-suggest")
    def _collect_file_suggestions(self, cwd: str, picker: FileSuggest) -> None:
        root = Path(cwd)
        context = _file_index_context(root, self._protected_paths, self._adopted_auth_paths)
        paths = collect_paths(FileIndexConfig(root=root, context=context))
        # Hop back to the event loop: widget state must not be mutated from a worker.
        self.call_from_thread(self._install_file_suggestions, picker, paths)

    def _install_file_suggestions(self, picker: FileSuggest, paths: tuple[str, ...]) -> None:
        """Install the corpus and re-evaluate any mention the user already typed.

        The walk takes hundreds of milliseconds, so the user can easily type `@query`
        before it lands. `show_for` hid the menu at the time because the corpus was
        empty, and `set_paths` alone would not re-read the editor — the menu would
        stay hidden until the next keystroke. Re-running the trigger scan here makes
        simply waiting for indexing sufficient.

        The scan is a no-op unless the caret is currently inside a mention, so this
        never opens the menu on its own.
        """

        picker.set_paths(paths)
        editor = self._input
        if editor is None or not paths:
            return
        # An overlay or pending operation has already torn the composer down. The
        # worker is a background arrival, not user intent, so it must never revive
        # a composer-anchored menu on top of an active approval or picker.
        controller = self._overlay_controller
        if controller is not None and (
            controller.active_overlay is not None or controller.active_operation is not None
        ):
            return
        # Same precedence as on_text_area_changed: a live slash menu owns the line.
        if self._suggest is not None and self._suggest.is_open:
            return
        picker.show_for(editor.text, self._file_offset_of_cursor(editor))

    def on_transcript_follow_changed(self, event: Transcript.FollowChanged) -> None:
        if event.following:
            if self._oldest_navigation_generation is not None:
                # Window reconciliation can briefly collapse max_scroll_y to zero,
                # which looks like tail-following even though Home still owns the
                # navigation. Preserve that explicit reader intent; End cancels the
                # generation before it deliberately restores tail following.
                if self._transcript is not None:
                    self._transcript.stop_following()
                return
            self._cancel_oldest_navigation()
            show_latest = self._history_window_latest_hook
            if show_latest is not None:
                show_latest()
            self._transcript_controller.clear_unseen_output()
            self._stream.resume_if_deferred()
            self._request_live_history_reload()

    def on_transcript_need_more_history(self, event: Transcript.NeedMoreHistory) -> None:
        event.stop()
        transcript = self._transcript
        if (
            transcript is not None
            and event.navigation.reader_generation != transcript.follow_generation
        ):
            transcript.history_page_request_failed()
            pending = self._pending_history_navigation
            if pending.reader_generation == transcript.follow_generation:
                transcript.request_history_at_top(pending)
            return
        if (
            event.navigation.intent is not HistoryNavigationIntent.PRESERVE
            and event.navigation != self._pending_history_navigation
        ):
            if transcript is not None:
                transcript.history_page_request_failed()
            return
        self._pending_history_navigation = event.navigation
        shift_older = self._history_window_older_hook
        if shift_older is not None and shift_older():
            if transcript is not None:
                transcript.history_page_request_failed()
            return
        hook = self._history_page_request_hook
        if hook is None or transcript is None or not transcript.has_more_history:
            self._pending_history_navigation = HistoryNavigation()
            if transcript is not None:
                transcript.history_page_request_failed()
            if event.navigation.intent is HistoryNavigationIntent.OLDEST:
                self._oldest_navigation_generation = None
            return
        self.run_worker(
            hook(),
            group="history-page-request",
            exit_on_error=False,
        )

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
            transcript = self._transcript
            if transcript is not None and not self.is_newest_transcript_widget(event.widget):
                # An active stream may have Textual's same-pass anchor armed.
                # Release it before an older focused card can expand and reflow.
                transcript.anchor(False)

    def on_tool_card_toggled(self, event: ToolCard.Toggled) -> None:
        # A card grew or shrank. Re-pin the tail only when the *newest* card (the
        # transcript's last child) is expanded while the reader was following: that
        # keeps its output in view even though focusing a tall card scrolled the tail
        # off first. Expanding an older card, or one the reader scrolled up to reach,
        # leaves the viewport alone so the freshly revealed content isn't yanked away.
        event.stop()
        transcript = self._transcript
        if transcript is not None and not self.is_newest_transcript_widget(event.card):
            # A provider delta may have re-armed the stream anchor after this
            # older card received focus. Disarm it again before the toggle's
            # invalidated layout is composed.
            transcript.anchor(False)
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

    def on_tool_card_view_diff_requested(self, event: ToolCard.ViewDiffRequested) -> None:
        """Open a card's retained structured diff without disturbing scrollback."""

        event.stop()
        self.show_diff_viewer(event.presentation)

    def on_diff_viewer_closed(self, event: DiffViewer.Closed) -> None:
        event.stop()
        self.hide_diff_viewer()

    def _cancel_card_expand_repin(self) -> None:
        # A user scroll after focusing a card is a deliberate move away from the tail.
        self._transcript_controller.user_scrolled()

    def _begin_transcript_navigation(self, *, preserve_live_history_recovery: bool = False) -> int:
        """Invalidate stale viewport work and cancel incompatible reader actions."""

        self._transcript_navigation_generation += 1
        self._cancel_oldest_navigation()
        if not preserve_live_history_recovery:
            self._live_history_recovery_navigation = None
            self._live_history_recovery_blocked = False
        return self._transcript_navigation_generation

    def _cancel_oldest_navigation(self) -> None:
        self._oldest_navigation_generation = None
        self._pending_history_navigation = HistoryNavigation()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self._wheel_event_targets_transcript(event):
            self._cancel_card_expand_repin()
            assert self._transcript is not None
            transcript = self._transcript
            anchor = self._history_prepend_anchor
            continuing_prepend = (
                anchor is not None
                and anchor.transcript is transcript
                and anchor.navigation.intent is not HistoryNavigationIntent.OLDEST
            )
            continuing_load = transcript.history_page_loading
            if not continuing_prepend and not continuing_load:
                self._begin_transcript_navigation(preserve_live_history_recovery=True)
            navigation = transcript.prepare_wheel_up(request_history=not continuing_prepend)
            if navigation is not None:
                if continuing_prepend:
                    assert anchor is not None
                    anchor.navigation = _merge_history_navigation(anchor.navigation, navigation)
                elif continuing_load:
                    self._pending_history_navigation = _merge_history_navigation(
                        self._pending_history_navigation,
                        navigation,
                    )
                else:
                    self._pending_history_navigation = navigation
                self._recover_evicted_history_for_backward_navigation(navigation)
        self._forward_jump_overlay_scroll(event, direction=-1)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if self._wheel_event_targets_transcript(event):
            self._cancel_card_expand_repin()
            self._begin_transcript_navigation()
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
        """Submit a typed slash-command line through the input controller."""

        if self._submit_local_theme_command(text):
            self.clear_prompt_editor()
            return
        self._input_controller.submit_line(text, clear_editor=True)

    def _submit_local_theme_command(self, text: str) -> bool:
        """Handle Textual-only ``/theme`` without crossing the RPC boundary."""

        if "\n" in text or "\r" in text:
            return False
        parts = text.strip().split()
        if not parts or parts[0].casefold() != "/theme":
            return False
        if len(parts) == 1:
            self.show_theme_picker()
            return True
        if len(parts) != 2:
            self.write_error("Usage: /theme [vapor|orchid|ember|paper]")
            return True
        spec = WISP_THEME_BY_SLUG.get(parts[1].casefold())
        if spec is None:
            self.write_error(f"Unknown theme: {parts[1]}")
            return True
        self._commit_theme(spec.name, announce=True)
        return True

    def _submit_decision_line(self, text: str) -> bool:
        # The decision overlay temporarily hides the composer. Keep its draft
        # untouched so approval never discards a follow-up the user was typing.
        return self._input_controller.submit_line(text, clear_editor=False)

    def on_decision_panel_selected(self, event: DecisionPanel.Selected) -> None:
        event.stop()
        self._submit_decision_line(event.answer)

    def on_connect_panel_method_selected(self, event: ConnectPanel.MethodSelected) -> None:
        event.stop()
        hook = self._connect_oauth_hook
        if hook is None:
            self.hide_connect_panel()
            return
        self.run_worker(
            hook(event.method.provider),
            group="connect-oauth",
            exclusive=True,
            exit_on_error=False,
        )

    def on_connect_panel_api_key_submitted(self, event: ConnectPanel.ApiKeySubmitted) -> None:
        event.stop()
        panel = self._connect_panel
        hook = self._connect_api_key_hook
        api_key = panel.take_api_key() if panel is not None else None
        if hook is None or api_key is None:
            self.hide_connect_panel()
            return
        self.run_worker(
            hook(event.provider, api_key),
            group="connect-api-key",
            exit_on_error=False,
        )

    def on_connect_panel_disconnect_selected(self, event: ConnectPanel.DisconnectSelected) -> None:
        event.stop()
        if not self._submit_decision_line(f"/disconnect {event.provider}"):
            self.hide_connect_panel()

    def on_connect_panel_cancelled(self, event: ConnectPanel.Cancelled) -> None:
        event.stop()
        self.workers.cancel_group(self, "connect-oauth")
        self.hide_connect_panel()

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

    def on_theme_picker_previewed(self, event: ThemePicker.Previewed) -> None:
        event.stop()
        if self._theme_picker_original is None:
            return
        self._pending_theme_preview = event.theme_name
        if self._theme_preview_scheduled:
            return
        self._theme_preview_scheduled = True
        epoch = self._theme_preview_epoch
        self.call_after_refresh(self._flush_theme_preview, epoch)

    def on_theme_picker_selected(self, event: ThemePicker.Selected) -> None:
        event.stop()
        overlays = self._overlay_controller
        if (
            overlays is None
            or overlays.active_overlay is not OverlayKind.theme_picker
            or self._theme_picker_original is None
        ):
            return
        self._invalidate_theme_preview()
        self._commit_theme(event.theme_name, announce=True)
        self._theme_picker_original = None
        self.hide_theme_picker()

    def on_theme_picker_cancelled(self, event: ThemePicker.Cancelled) -> None:
        event.stop()
        self._rollback_theme_picker_preview()
        self.hide_theme_picker()

    def _rollback_theme_picker_preview(self) -> None:
        original = self._theme_picker_original
        self._invalidate_theme_preview()
        self._theme_picker_original = None
        if original in WISP_THEME_NAMES:
            self.theme = original

    def _on_overlay_displaced(self, kind: OverlayKind) -> None:
        if kind is OverlayKind.theme_picker:
            self._rollback_theme_picker_preview()

    def _flush_theme_preview(self, epoch: int) -> None:
        if epoch != self._theme_preview_epoch:
            return
        self._theme_preview_scheduled = False
        theme_name, self._pending_theme_preview = self._pending_theme_preview, None
        if theme_name in WISP_THEME_NAMES and self._theme_picker_original is not None:
            self.theme = theme_name

    def _invalidate_theme_preview(self) -> None:
        self._theme_preview_epoch += 1
        self._pending_theme_preview = None
        self._theme_preview_scheduled = False

    def on_context_status_overlay_cancelled(self, event: ContextStatusOverlay.Cancelled) -> None:
        event.stop()
        self.hide_context_status()

    def set_command_catalog(self, catalog: TuiCommandCatalog) -> None:
        """Apply the executable catalog to inline slash suggestions."""

        self._command_catalog = catalog
        presentation_catalog = catalog.with_descriptors(*TEXTUAL_LOCAL_COMMAND_DESCRIPTORS)
        if self._suggest is not None:
            self._suggest.set_catalog(presentation_catalog)
            # Catalog discovery can complete just after the user starts typing at
            # startup. Reproject the current editor value so an already-open `/`
            # menu is refreshed instead of being dismissed by the catalog swap.
            if self._input is not None and self._input.display:
                self._suggest.show_for(self._input.text)

    def set_skill_catalog(self, catalog: RpcSkillCatalogSnapshot) -> None:
        """Apply the immutable skill snapshot to inline completion."""

        self._skill_catalog = catalog
        if self._suggest is not None:
            self._suggest.set_skill_catalog(catalog)
            if self._input is not None and self._input.display:
                self._suggest.show_for(self._input.text)

    def show_skill_catalog(self, catalog: RpcSkillCatalogSnapshot) -> None:
        """Mount a literal-text catalog inspection block."""

        self.write_message(skill_catalog_text(catalog), role="system")

    def show_skill_invocation(
        self,
        event: SkillInvoked,
        *,
        widget: LineMessage | None = None,
    ) -> None:
        """Replace a raw live invocation echo with its typed compact label."""

        text = skill_invocation_text(event)
        if widget is not None:
            widget.update(text)
            self.note_transcript_update(widget)
            return
        self.write_message(text, role="user")

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
        # Menu-scoped keys, handled only while a menu is open so normal input (Tab
        # focus, Escape, arrows in the editor) is untouched otherwise. Enter is
        # intentionally NOT intercepted — on_input_submitted runs the line, and
        # _accept_menu_highlight_on_enter decides whether a menu claims it first.
        file_suggest = self._file_suggest
        if file_suggest is not None and file_suggest.is_open:
            if event.key in {"down", "up", "tab", "escape"}:
                if event.key == "down":
                    file_suggest.action_cursor_down()
                elif event.key == "up":
                    file_suggest.action_cursor_up()
                elif event.key == "tab":
                    self._complete_path_from_menu()
                else:
                    # Dismiss but keep whatever the user typed.
                    file_suggest.hide()
                    self.refresh_bindings()
                event.prevent_default()
                event.stop()
            return

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
            self.refresh_bindings()
            event.prevent_default()
            event.stop()

    def _suggestion_menu_is_open(self) -> bool:
        return bool(
            (self._file_suggest is not None and self._file_suggest.is_open)
            or (self._suggest is not None and self._suggest.is_open)
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {"menu_move", "menu_complete"}:
            return self._suggestion_menu_is_open()
        return super().check_action(action, parameters)

    def action_menu_move(self, direction: int) -> None:
        menu = (
            self._file_suggest
            if self._file_suggest is not None and self._file_suggest.is_open
            else self._suggest
        )
        if menu is None or not menu.is_open:
            return
        if direction < 0:
            menu.action_cursor_up()
        else:
            menu.action_cursor_down()

    def action_menu_complete(self) -> None:
        if self._file_suggest is not None and self._file_suggest.is_open:
            self._complete_path_from_menu()
        else:
            self._complete_from_menu()
        self.refresh_bindings()

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
        self.refresh_bindings()

    def _complete_path_from_menu(self) -> bool:
        """Replace the in-progress `@query` with the highlighted path.

        Unlike `_complete_from_menu`, this must NOT use `prefill_command` — that
        replaces the whole buffer, which is right for a slash command that owns the
        line but would destroy the surrounding prose of a mid-prompt mention. Only
        the `@…` span itself is spliced, and the caret lands after the inserted
        path so typing continues naturally.

        Returns whether a completion was applied.
        """

        picker = self._file_suggest
        editor = self._input
        if picker is None or editor is None or not picker.is_open:
            return False
        path = picker.highlighted_path()
        if path is None:
            return False

        value = editor.text
        cursor = self._file_offset_of_cursor(editor)
        query = picker.query_from_value(value, cursor)
        if query is None:
            return False

        # The mention spans from its `@` through the fragment typed so far. A path
        # containing a space would break the single-token grammar the trigger
        # relies on, so quote it — mirroring how Toad emits `@"my file.py"`.
        start = cursor - len(query) - 1
        rendered = f'@"{path}"' if " " in path else f"@{path}"
        replacement = f"{rendered} "
        editor.value = f"{value[:start]}{replacement}{value[cursor:]}"
        editor.cursor_position = start + len(replacement)
        picker.hide()
        self.refresh_bindings()
        return True

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
        # Textual must enable terminal mouse reporting for wheel/trackpad events to
        # reach the Transcript. Keep this explicit: the default is also True, but
        # silently reverting to mouse=False breaks real-terminal scrolling while
        # headless widget tests continue to pass.
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
            try:
                await self._stream.shutdown()
            finally:
                self.exit()

    async def close(self) -> None:
        try:
            await self._stream.shutdown()
        finally:
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
        self._signal_input(TuiQuitRequested(), action="quit")

    def action_toggle_contextual_help(self) -> None:
        """Toggle Textual's focus-aware help without moving keyboard focus."""

        if not self.is_running:
            return
        if self.screen.query(HelpPanel):
            transcript = self._transcript
            viewport_state = self._help_viewport_state
            baseline = self._help_viewport_baseline
            should_restore = bool(
                transcript is not None
                and viewport_state is not None
                and (baseline is None or transcript.viewport_state() == baseline)
            )
            self.action_hide_help_panel()
            self._help_viewport_state = None
            self._help_viewport_baseline = None
            if should_restore and transcript is not None and viewport_state is not None:
                self.call_after_refresh(transcript.restore_viewport_state, viewport_state)
            return

        if self._transcript is not None:
            self._help_viewport_state = self._transcript.viewport_state()
            self._help_viewport_baseline = None
        self.action_show_help_panel()
        if self._transcript is not None and self._help_viewport_state is not None:
            self.call_after_refresh(self._stabilize_help_viewport)

    def _stabilize_help_viewport(self) -> None:
        transcript = self._transcript
        viewport_state = self._help_viewport_state
        if transcript is None or viewport_state is None or not self.screen.query(HelpPanel):
            return
        transcript.restore_viewport_state(viewport_state)
        self._help_viewport_baseline = transcript.viewport_state()

    def _help_key_panel(self) -> KeyPanel | None:
        if not self.is_running:
            return None
        panels = self.screen.query(HelpPanel)
        if not panels:
            return None
        return panels.first().query_one(KeyPanel)

    def action_cancel(self) -> None:
        """Dismiss the nearest UI layer, then fall back to shell cancellation."""

        file_suggest = self._file_suggest
        if file_suggest is not None and file_suggest.is_open:
            file_suggest.hide()
            self.refresh_bindings()
            return
        suggest = self._suggest
        if suggest is not None and suggest.is_open:
            suggest.hide()
            self.refresh_bindings()
            return
        overlays = self._overlay_controller
        if overlays is not None and overlays.active_overlay is OverlayKind.connect:
            self.workers.cancel_group(self, "connect-oauth")
        if overlays is not None and overlays.consume_cancel():
            return
        self._signal_input(TuiCancelRequested(), action="cancel", clear_editor=False)

    def action_eof(self) -> None:
        editor = self._input
        if editor is not None and editor.text:
            # A draft remains authoritative even while an overlay temporarily
            # hides the composer; Ctrl+D must never turn that non-empty state into
            # EOF merely because focus moved.
            editor.action_delete_right()
            return
        self._signal_input(EOFError(), action="EOF")

    def action_toggle_agent_mode(self) -> None:
        """Route the plan/build hotkey through the normal slash-command path."""

        command = "/build" if self._agent_mode == "plan" else "/plan"
        self._input_controller.submit_line(command, clear_editor=False)

    def action_toggle_theme(self) -> None:
        """Toggle Paper against the most recently committed dark theme."""

        if self._theme_picker_original is not None:
            return
        next_name = self._last_dark_theme if self.theme == PAPER_THEME_NAME else PAPER_THEME_NAME
        self._commit_theme(next_name, announce=True)

    def _commit_theme(self, theme_name: str, *, announce: bool) -> None:
        """Apply and persist one validated curated theme."""

        spec = WISP_THEME_BY_NAME.get(theme_name)
        if spec is None:
            return
        self.theme = spec.name
        if spec.dark:
            self._last_dark_theme = spec.name
        saved = save_theme_state(
            ThemePreferenceState(
                active_theme=spec.name,
                last_dark_theme=self._last_dark_theme,
            )
        )
        if announce:
            suffix = "" if saved else " (could not save)"
            self.write_notice(f"Theme: {spec.label}{suffix}")

    def show_theme_picker(self) -> None:
        picker = self._theme_picker
        overlays = self._overlay_controller
        if picker is None or overlays is None:
            return
        if picker.is_open:
            self.on_theme_picker_cancelled(ThemePicker.Cancelled())
            return
        self._theme_picker_original = self.theme
        self._invalidate_theme_preview()
        overlays.open(OverlayKind.theme_picker, preserve_viewport=True)
        picker.show(self.theme)

    def hide_theme_picker(self) -> bool:
        overlays = self._overlay_controller
        return overlays is not None and overlays.close(OverlayKind.theme_picker)

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
        editor_top = self._input.region.y
        overlays.open(OverlayKind.prompt_history, preserve_viewport=True)
        picker.styles.max_height = max(4, editor_top)
        picker.styles.offset = (0, "-100%")
        picker.show(self._input_controller.prompt_history_entries)

        def anchor_above_editor() -> None:
            overlap = picker.region.bottom - editor_top
            if overlap > 0:
                picker.styles.offset = (0, -picker.region.height - overlap)

        self.call_after_refresh(anchor_above_editor)

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
        if self._diff_viewer is not None and self._diff_viewer.is_open:
            self._diff_viewer.action_page_up()
            return
        if help_keys := self._help_key_panel():
            help_keys.action_page_up()
            return
        if self._decision_panel is not None and self._decision_panel.is_open:
            self._decision_panel.move_highlight_page_up()
            return
        if self._prompt_history_picker is not None and self._prompt_history_picker.is_open:
            self._prompt_history_picker.move_highlight_page_up()
            return
        if self._session_picker is not None and self._session_picker.is_open:
            self._session_picker.move_highlight_page_up()
            return
        self._cancel_card_expand_repin()
        if self._transcript is not None:
            self._begin_transcript_navigation(preserve_live_history_recovery=True)
            navigation = self._transcript.page_up()
            if navigation is not None:
                self._pending_history_navigation = navigation
                self._recover_evicted_history_for_backward_navigation(navigation)

    def action_scroll_transcript_page_down(self) -> None:
        if self._diff_viewer is not None and self._diff_viewer.is_open:
            self._diff_viewer.action_page_down()
            return
        if help_keys := self._help_key_panel():
            help_keys.action_page_down()
            return
        if self._decision_panel is not None and self._decision_panel.is_open:
            self._decision_panel.move_highlight_page_down()
            return
        if self._prompt_history_picker is not None and self._prompt_history_picker.is_open:
            self._prompt_history_picker.move_highlight_page_down()
            return
        if self._session_picker is not None and self._session_picker.is_open:
            self._session_picker.move_highlight_page_down()
            return
        self._cancel_card_expand_repin()
        if self._transcript is not None:
            self._begin_transcript_navigation()
            self._transcript.page_down()

    def action_scroll_transcript_home(self) -> None:
        if self._diff_viewer is not None and self._diff_viewer.is_open:
            self._diff_viewer.action_home()
            return
        if help_keys := self._help_key_panel():
            help_keys.action_scroll_home()
            return
        if self._decision_panel is not None and self._decision_panel.is_open:
            self._decision_panel.move_highlight_first()
            return
        if self._prompt_history_picker is not None and self._prompt_history_picker.is_open:
            self._prompt_history_picker.move_highlight_first()
            return
        if self._session_picker is not None and self._session_picker.is_open:
            self._session_picker.move_highlight_first()
            return
        self._cancel_card_expand_repin()
        if self._transcript is not None:
            generation = self._begin_transcript_navigation()
            self._oldest_navigation_generation = generation
            self._transcript.scroll_to_oldest()
            self._continue_oldest_navigation(generation, self._transcript_epoch)

    def action_scroll_transcript_end(self) -> None:
        if self._diff_viewer is not None and self._diff_viewer.is_open:
            self._diff_viewer.action_end()
            return
        if help_keys := self._help_key_panel():
            help_keys.action_scroll_end()
            return
        if self._decision_panel is not None and self._decision_panel.is_open:
            self._decision_panel.move_highlight_last()
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
            self._begin_transcript_navigation()
            self._transcript.return_to_latest()
        self._transcript_controller.clear_unseen_output()

    def _signal_input(
        self,
        signal: BaseException,
        *,
        action: str,
        clear_editor: bool = True,
    ) -> None:
        # Pending compact echoes remain intact here. Control signals can merely
        # affect presentation or arm quit; the shell reclaims echoes only when it
        # definitively drops queued follow-ups.
        self._input_controller.signal(
            signal,
            action=action,
            clear_editor=clear_editor,
        )

    def set_status(self, snapshot: TuiViewSnapshot) -> None:
        self._agent_mode = snapshot.mode
        if self._composer is not None:
            self._composer.set_snapshot(snapshot)
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

    def show_connect_panel(
        self,
        providers: tuple[ConnectionProviderStatus, ...],
        *,
        mode: ConnectPanelMode = "connect",
        provider: str | None = None,
    ) -> None:
        panel = self._connect_panel
        overlays = self._overlay_controller
        if panel is None or overlays is None:
            return
        overlays.open(OverlayKind.connect)
        panel.show(
            providers,
            mode=mode,
            provider=provider,
        )

    def show_connect_device_code(self, verification_uri: str, user_code: str) -> None:
        panel = self._connect_panel
        if panel is None or not panel.is_open:
            return
        panel.query_one("#connect-title", Static).update("Authorize ChatGPT Plus/Pro")
        panel.query_one("#connect-hint", Static).update(
            f"{verification_uri} · code {user_code} · esc cancel"
        )

    def show_connect_error(self, message: str) -> None:
        panel = self._connect_panel
        if panel is not None and panel.is_open:
            panel.show_error(message)

    def hide_connect_panel(self) -> None:
        overlays = self._overlay_controller
        if overlays is not None:
            overlays.close(OverlayKind.connect)

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

    def show_diff_viewer(self, presentation: DiffPresentation) -> None:
        """Show one tool-card diff with an independently scrollable full view."""

        viewer = self._diff_viewer
        overlays = self._overlay_controller
        if viewer is None or overlays is None:
            return
        if overlays.active_overlay is not None or overlays.active_operation is not None:
            return
        overlays.open(OverlayKind.diff_viewer, preserve_viewport=True)
        viewer.show_diff(presentation)

    def hide_diff_viewer(self) -> bool:
        """Dismiss the diff reader and restore transcript position and composer."""

        overlays = self._overlay_controller
        if overlays is None:
            return False
        return overlays.close(OverlayKind.diff_viewer)

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
        self._live_history_eviction_generation = 0
        self._live_history_reload_generation = None
        self._live_history_recovery_navigation = None
        self._live_history_recovery_blocked = False
        self._history_prepend_mounts.clear()
        self._history_prepend_anchor = None
        self._cancel_oldest_navigation()
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
        show_oldest: Callable[[], bool],
        show_latest: Callable[[], bool],
    ) -> None:
        """Install renderer-owned history-window navigation callbacks."""

        self._history_window_older_hook = shift_older
        self._history_window_oldest_hook = show_oldest
        self._history_window_latest_hook = show_latest

    def set_connect_api_key_hook(
        self,
        hook: Callable[[str, str], Awaitable[None]],
    ) -> None:
        """Install the shell-owned secret-storage callback."""

        self._connect_api_key_hook = hook

    def set_connect_oauth_hook(
        self,
        hook: Callable[[str], Awaitable[None]],
    ) -> None:
        """Install the shell-owned device-authorization callback."""

        self._connect_oauth_hook = hook

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
        self._live_history_eviction_generation += 1
        self._live_history_reload_needed = True
        self._request_live_history_reload()

    def _recover_evicted_history_for_backward_navigation(
        self,
        navigation: HistoryNavigation,
    ) -> None:
        """Load a durable prefix when PageUp or the wheel reaches live eviction.

        Ordinary history paging owns retained and server-known older pages. This
        path is only for a live transcript whose oldest mounted widgets were
        evicted before the reader started browsing, leaving no page cursor for
        the normal edge handler to follow.
        """

        transcript = self._transcript
        request_latest = self._history_latest_request_hook
        if (
            not self._live_history_reload_needed
            or request_latest is None
            or transcript is None
            or transcript.is_following
            or transcript.scroll_y != 0
            or transcript.can_page_to_older_history
            or self._live_history_recovery_blocked
        ):
            return
        pending = self._live_history_recovery_navigation
        if pending is not None:
            # Wheel events can arrive while the RPC page is in flight. Retain the
            # unconsumed distance so the restored viewport lands where the reader
            # intended, rather than making them repeat the burst after every page.
            self._live_history_recovery_navigation = HistoryNavigation(
                navigation.intent,
                pending.remaining_rows + navigation.remaining_rows,
                transcript.follow_generation,
            )
            return
        if self._live_history_reload_pending:
            return
        self._live_history_recovery_navigation = HistoryNavigation(
            navigation.intent,
            navigation.remaining_rows,
            transcript.follow_generation,
        )
        self._start_live_history_reload(request_latest)

    def consume_live_history_recovery(self) -> HistoryNavigation | None:
        """Return a still-valid backward recovery intent for the renderer."""

        navigation = self._live_history_recovery_navigation
        transcript = self._transcript
        self._live_history_recovery_navigation = None
        if (
            navigation is None
            or transcript is None
            or transcript.is_following
            or transcript.scroll_y != 0
        ):
            return None
        navigation = HistoryNavigation(
            navigation.intent,
            navigation.remaining_rows,
            transcript.follow_generation,
        )
        self._pending_history_navigation = navigation
        return navigation

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
        self._start_live_history_reload(request_latest)

    def _start_live_history_reload(self, request_latest: Callable[[], Awaitable[None]]) -> None:
        """Start one serialized reload."""

        self._live_history_reload_pending = True
        self.run_worker(
            request_latest(),
            group="history-latest-reload",
            exit_on_error=False,
        )

    def capture_live_history_reload(self) -> None:
        """Record the evictions covered when the durable request actually starts."""

        self._live_history_reload_generation = self._live_history_eviction_generation

    def live_history_reloaded(self) -> None:
        """Finish one reload and repeat it if newer output was evicted in flight."""

        covered_generation = self._live_history_reload_generation
        self._live_history_reload_pending = False
        self._live_history_reload_generation = None
        self._live_history_reload_needed = (
            covered_generation is not None
            and covered_generation != self._live_history_eviction_generation
        )
        self._live_history_recovery_navigation = None
        self._live_history_recovery_blocked = False
        self._request_live_history_reload()

    def live_history_recovery_deferred(self) -> None:
        """Release an unsafe oldest-window recovery without losing tail reload work."""

        self._live_history_reload_pending = False
        self._live_history_reload_generation = None
        self._live_history_recovery_navigation = None
        self._live_history_recovery_blocked = True

    def live_history_reload_failed(self) -> None:
        """Release a failed request while retaining recovery work for a later retry."""

        self._live_history_reload_pending = False
        self._live_history_reload_generation = None
        self._live_history_recovery_navigation = None

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
        """Return whether ``widget`` is the latest durable transcript message."""

        transcript = self._transcript
        if transcript is None:
            return False
        indicator = self._transcript_controller.working_indicator
        return (
            next(
                (child for child in reversed(transcript.children) if child is not indicator),
                None,
            )
            is widget
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

    def show_activity_indicator(self, label: str) -> None:
        self._transcript_controller.show_activity_indicator(label)

    def restart_working_indicator(self) -> None:
        """Start fresh transcript activity for a newly submitted prompt."""

        self._transcript_controller.restart_working_indicator()

    def hide_working_indicator(self) -> None:
        self._transcript_controller.hide_working_indicator()

    def hide_working_indicator_after_stream(self) -> None:
        """Remove the current heartbeat with the completed stream's final layout."""

        indicator = self._transcript_controller.working_indicator
        if indicator is None:
            return

        def hide_if_current() -> None:
            self._transcript_controller.hide_working_indicator_if_current(indicator)

        if not self._stream.defer_until_latest_stream_settles(hide_if_current):
            hide_if_current()

    def mount_tool_call(
        self,
        call_id: str,
        name: str,
        arguments: object,
        *,
        historical_card_id: str | None = None,
        historical: bool = False,
        arguments_available: bool = True,
        before: Widget | None = None,
    ) -> ToolCard | None:
        """Mount and register one evolving live or retained-history tool card."""

        return self._transcript_controller.mount_tool_call(
            call_id,
            name,
            arguments,
            historical_card_id=historical_card_id,
            historical=historical,
            arguments_available=arguments_available,
            before=before,
        )

    def enrich_historical_tool_call(
        self,
        card_id: str,
        name: str,
        arguments: object,
        *,
        status: ToolActionStatus,
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
        status: ToolActionStatus,
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

    def _write_styled(self, role: str, message: str) -> None:
        self._mount_line(role, message)

    def write_notice(self, message: str) -> None:
        self._write_styled("notice", message)

    def write_error(self, message: str) -> None:
        self._write_styled("error", message)

    def write_dim(self, message: str) -> None:
        self._write_styled("dim", message)

    def mount_history_marker(self, message: str, *, before: Widget | None) -> None:
        """Mount the durable boundary between the session label and history."""

        widget = LineMessage(message, role="dim")
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
        return self._mount_line("user", message)

    def write_assistant(self, message: str) -> StreamMessage | None:
        """Mount a settled assistant turn through the same Markdown path as streaming."""

        if self._transcript is None:
            return None
        widget = StreamMessage(message)
        self._mount_transcript_message(widget)
        self._transcript_controller.settle_widget(widget, durable_entry_count=1)
        self.note_transcript_update(widget)
        self._follow_tail_after_refresh()
        return widget

    def write_message(self, message: str, *, role: str) -> Widget | None:
        # Assistant content is Markdown whether it streamed live or arrived as one
        # completed event. Other roles remain literal at the widget boundary.
        if role == "assistant":
            return self.write_assistant(message)
        return self._mount_line(role, message)

    def _mount_line(self, role: str, text: str) -> LineMessage | None:
        # Mount one role-styled LineMessage. Transcript owns the transcript and
        # its own follow-the-tail intent; we just re-assert the follow after the
        # mount lays out.
        if self._transcript is None:
            return None
        widget = LineMessage(text, role=role)
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
    ) -> Widget | None:
        """Mount one retained history message without treating it as new output."""

        if self._transcript is None:
            return None
        widget: Widget = (
            StreamMessage(message)
            if role == "assistant" and _historical_message_needs_markdown(message)
            else LineMessage(message, role=role)
        )
        self._mount_transcript_message(widget, before=before)
        return widget

    def _mount_transcript_message(
        self,
        widget: Widget,
        *,
        before: Widget | None = None,
    ) -> AwaitMount | None:
        transcript = self._transcript
        if transcript is None:
            return None
        anchor = self._history_prepend_anchor
        anchor_boundary = (
            anchor.widget
            if (
                self._prepending_history
                and anchor is not None
                and anchor.transcript is transcript
                and anchor.widget is not None
                and anchor.widget.parent is transcript
            )
            else None
        )
        # Textual queues mounts until the next message-pump cycle. Reconciliation
        # can therefore offer a widget mounted earlier in this batch as the next
        # insertion boundary even though it has no parent yet. Textual rejects
        # such a relative mount, so fall back to the stable prepend anchor.
        indicator = self._transcript_controller.working_indicator
        live_boundary = (
            indicator
            if before is None and widget is not indicator and indicator is not None
            else None
        )
        mount_before = next(
            (
                candidate
                for candidate in (before, live_boundary, anchor_boundary)
                if candidate is not None and candidate.parent is transcript
            ),
            None,
        )
        mounted = transcript.mount_message(widget, before=mount_before)
        if self._prepending_history:
            self._history_prepend_mounts.append(mounted)
        if self._history_render_depth:
            self._history_render_mounts.append(mounted)
        if live_boundary is not None and live_boundary.parent is not transcript:
            # Both mounts may be queued in one message-pump turn. Once they settle,
            # restore the heartbeat as the tail without unmounting or resetting it.
            self.call_after_refresh(self._move_working_indicator_to_tail)
        return mounted

    def mount_stream_widget(self, widget: StreamMessage) -> AwaitMount:
        """Mount a streaming assistant turn before the persistent heartbeat."""

        mounted = self._mount_transcript_message(widget)
        if mounted is None:
            raise RuntimeError("cannot mount a stream without a transcript")
        return mounted

    def _move_working_indicator_to_tail(self) -> None:
        transcript = self._transcript
        indicator = self._transcript_controller.working_indicator
        if (
            transcript is None
            or indicator is None
            or indicator.parent is not transcript
            or not transcript.children
            or transcript.children[-1] is indicator
        ):
            return
        transcript.move_child(indicator, after=transcript.children[-1])

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
        if not has_more and self._oldest_navigation_generation is None:
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
        oldest_generation = self._oldest_navigation_generation
        if (
            oldest_generation is not None
            and oldest_generation == self._transcript_navigation_generation
        ):
            transcript.history_page_layout_settled()
            self._continue_oldest_navigation(oldest_generation, epoch)
            return
        # Pin the initial history batch before arming edge pagination. Under a
        # delayed Textual layout, arming first can let a pending top-edge watcher
        # request an unnecessary page before follow_tail() reaches the real end.
        transcript.follow_tail()
        transcript.history_page_layout_settled()
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
        if not (
            generation == self._history_layout_generation
            and epoch == self._transcript_epoch
            and transcript is self._transcript
        ):
            return
        viewport_height = transcript.scrollable_content_region.height
        if viewport_height <= 0:
            self.call_after_refresh(
                self._request_history_if_still_at_top,
                transcript,
                generation,
                epoch,
            )
            return
        # A mounted widget can legitimately occupy zero rows (for example, a
        # whitespace-only Markdown message), so its height cannot distinguish an
        # unsettled layout from stable empty output. Child count remains a
        # conservative lower bound while Textual updates virtual geometry.
        # Never request another page merely because scroll_y has not caught up.
        if (
            len(transcript.children) > viewport_height
            or transcript.virtual_size.height > viewport_height
            or transcript.max_scroll_y > 0
        ):
            return
        transcript.request_history_at_top()

    def _continue_oldest_navigation(self, generation: int, epoch: int) -> None:
        """Advance one retained or durable step toward the session beginning."""

        transcript = self._transcript
        if (
            generation != self._oldest_navigation_generation
            or generation != self._transcript_navigation_generation
            or epoch != self._transcript_epoch
            or transcript is None
            or transcript.is_following
        ):
            return
        self._pending_history_navigation = HistoryNavigation(
            HistoryNavigationIntent.OLDEST,
            reader_generation=transcript.follow_generation,
        )
        show_oldest = self._history_window_oldest_hook
        if show_oldest is not None and show_oldest():
            self.run_worker(
                self._continue_oldest_after_mounts(
                    transcript,
                    self._last_history_render_mounts,
                    generation,
                    epoch,
                ),
                group="history-oldest-navigation",
                exit_on_error=False,
            )
            return
        transcript.scroll_home(animate=False)
        if transcript.has_more_history:
            transcript.request_history_at_top(
                HistoryNavigation(
                    HistoryNavigationIntent.OLDEST,
                    reader_generation=transcript.follow_generation,
                )
            )
            return
        self._cancel_oldest_navigation()

    async def _continue_oldest_after_mounts(
        self,
        transcript: Transcript,
        mounts: tuple[AwaitMount, ...],
        generation: int,
        epoch: int,
    ) -> None:
        for mounted in mounts:
            await mounted
        self.call_after_refresh(
            self._continue_oldest_after_refresh,
            transcript,
            generation,
            epoch,
        )

    def _continue_oldest_after_refresh(
        self,
        transcript: Transcript,
        generation: int,
        epoch: int,
    ) -> None:
        if transcript is self._transcript:
            self._continue_oldest_navigation(generation, epoch)

    def history_page_request_failed(self) -> None:
        """Stop automatic Home traversal while leaving manual history retry armed."""

        self._cancel_oldest_navigation()
        transcript = self._transcript
        if transcript is not None:
            transcript.history_page_request_failed()

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
        navigation = self._pending_history_navigation
        self._pending_history_navigation = HistoryNavigation()
        self._history_prepend_anchor = _HistoryPrependAnchor(
            transcript=transcript,
            widget=first_history_entry,
            scroll_y=transcript.scroll_y,
            widget_y=first_history_entry.region.y if first_history_entry is not None else 0.0,
            following=transcript.is_following,
            epoch=self._transcript_epoch,
            navigation_generation=self._transcript_navigation_generation,
            navigation=navigation,
        )

    def finish_history_prepend(self) -> None:
        """Restore the reader's anchor after one older page has mounted."""

        self._prepending_history = False
        anchor = self._history_prepend_anchor
        mounts = tuple(self._history_prepend_mounts)
        self._history_prepend_mounts.clear()
        if anchor is not None:
            self.run_worker(
                self._restore_prepend_viewport_after_mounts(anchor, mounts),
                group="history-prepend",
                exit_on_error=False,
            )

    async def _restore_prepend_viewport_after_mounts(
        self,
        anchor: _HistoryPrependAnchor,
        mounts: tuple[AwaitMount, ...],
    ) -> None:
        for mounted in mounts:
            await mounted
        self.call_after_refresh(self._restore_prepend_viewport, anchor)

    def _restore_prepend_viewport(
        self,
        anchor: _HistoryPrependAnchor,
    ) -> None:
        if self._history_prepend_anchor is anchor:
            self._history_prepend_anchor = None
        transcript = anchor.transcript
        if (
            anchor.epoch != self._transcript_epoch
            or anchor.navigation_generation != self._transcript_navigation_generation
            or transcript is not self._transcript
            or transcript.is_following != anchor.following
            or (not anchor.following and transcript.scroll_y != anchor.scroll_y)
        ):
            return
        transcript.restore_prepend_viewport(
            scroll_y=anchor.scroll_y,
            anchor=anchor.widget,
            anchor_y_before=anchor.widget_y,
            following=anchor.following,
            navigation=anchor.navigation,
        )

    def append_stream(self, delta: str) -> None:
        self._stream.append(delta)

    def flush_stream(self, completed_content: str | None = None) -> None:
        self._stream.flush(completed_content)

    def stream_widget_for_completed_message(self) -> Widget | None:
        """Return the completed stream widget represented by a suppressed message event."""

        return self._stream.last_completed_widget

    @property
    def last_stream_write_count(self) -> int:
        """Return Markdown writes used by the latest completed stream turn."""

        return self._stream.last_completed_write_count

    async def wait_for_stream_idle(self) -> None:
        """Wait for scheduled Markdown rendering work to finish."""

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


def _historical_message_needs_markdown(message: str) -> bool:
    """Return whether Markdown can visibly differ from one literal transcript line.

    Plain one-line history stays lightweight: a retained window can contain hundreds
    of messages, and mounting a nested Markdown document for every literal sentence
    makes history paging prohibitively slow. Any multiline content or Markdown
    construct still uses the same Markdown widget as a live assistant turn.
    """

    if "\n" in message or "\r" in message or "\t" in message:
        return True
    if message.startswith("    ") or any(marker in message for marker in _MARKDOWN_VISIBLE_MARKERS):
        return True
    lowered = message.lower()
    if "http://" in lowered or "https://" in lowered or "www." in lowered:
        return True
    first, separator, _rest = message.lstrip().partition(" ")
    return bool(separator and first.rstrip(".)").isdigit())


def _file_index_context(
    root: Path,
    protected_paths: tuple[str, ...] | None = None,
    adopted_auth_paths: tuple[str, ...] = (),
) -> ToolContext:
    """Resolve the protected-path policy governing what the `@` picker may list.

    A bare ``ToolContext(cwd=root)`` would hardcode ``DEFAULT_PROTECTED_PATHS`` and
    silently ignore the user's real policy: a configured ``protected_paths`` entry
    (say ``secrets.yaml``) would be denied by the agent's tools but still offered
    for ``@``-mention here, and a nonstandard ``auth_path`` would lose the
    credential-file backstop that ``ToolContext.from_config`` guarantees.

    ``protected_paths`` is the parent's **already-resolved** policy and is preferred
    whenever the caller has one. Re-resolving instead would silently drop two things
    the parent alone knows: an ``--auth-file`` override (a CLI flag this process
    never sees again) and a trusted project's in-project ``auth_path`` (which
    ``WispConfig.from_env`` gates on ``trusted``). Either omission leaves the
    credential file indexable and offerable for mention while the agent's real tool
    context protects it.

    The fallback path exists for embedded callers that construct the app directly and
    have no resolved policy to hand over. There, resolving with ``trusted=False`` is
    deliberate: ``protected_paths`` is a user-scoped security setting that
    ``wisp.settings`` never reads from project-controlled config, so omitting the
    project layer cannot weaken the policy — at worst the picker hides a file the
    agent would have shown, which fails closed.

    ``adopted_auth_paths`` are credential files the session started protecting after
    startup, when a trusted project's config was applied mid-session. They union into
    whichever base policy the two branches above produced, since a deferred-trust
    approval moves the real auth file regardless of how the base was obtained.

    Config resolution touches the filesystem, so this runs on the picker's worker
    thread, never the event loop. A failure here must not take the TUI down: the
    picker is advisory, and falling back to the secure defaults keeps secrets hidden.
    """

    if protected_paths is not None:
        return ToolContext(
            cwd=root, protected_paths=_with_adopted(protected_paths, adopted_auth_paths)
        )
    try:
        config = WispConfig.from_env(project_dir=root)
        base = ToolContext.from_config(config, cwd=root)
    except Exception:
        base = ToolContext(cwd=root)
    # The adopted credential paths apply to the fallback branch too: a mid-session
    # trust approval moves the real auth file regardless of how the base policy
    # was obtained.
    if not adopted_auth_paths:
        return base
    return ToolContext(
        cwd=root, protected_paths=_with_adopted(base.protected_paths, adopted_auth_paths)
    )


def _with_adopted(base: tuple[str, ...], adopted: tuple[str, ...]) -> tuple[str, ...]:
    """Union the base policy with credential paths adopted after startup."""

    return (*base, *(pattern for pattern in adopted if pattern not in base))


def create_textual_tui(
    *, protected_paths: tuple[str, ...] | None = None
) -> tuple[TextualTui, TuiRenderer]:
    """Create a Textual app and renderer pair for `TuiShell`.

    ``protected_paths`` is the caller's already-resolved policy, forwarded to the
    `@`-picker so it hides exactly what the agent's tools deny. See
    ``_file_index_context`` for why re-deriving it here would be wrong.
    """

    app = TextualTui(protected_paths=protected_paths)
    return app, TextualTuiRenderer(app)


__all__ = ["TextualTui", "TextualTuiRenderer", "create_textual_tui"]
