"""Textual-based fullscreen TUI adapter for Wisp."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

import anyio
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, Input, Static

from wisp.events import (
    ErrorEvent,
    KnownWispEvent,
    MessageCompleted,
    RpcCommandFinished,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolResultReady,
    TrustRequested,
)
from wisp.tui.rendering import (
    TuiRenderer,
    TuiViewSnapshot,
    _first_line,
    _markup_escape,
    _tui_help_text,
    format_tui_footer_text,
)
from wisp.tui.theme import WISP_THEMES, role_styles
from wisp.tui.widgets import (
    DecisionPanel,
    LineMessage,
    SlashSuggest,
    StreamMessage,
    ToolCard,
    Transcript,
    WorkingMessage,
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
    "banner": "cyan",
}

# The Wisp wordmark, shown once at startup. Block-drawing glyphs only (width-1,
# universally supported); 40 cols wide, fits a standard terminal.
_WORDMARK = (
    "▄▄▄▄  ▄▄▄  ▄▄▄▄ ▄▄▄▄▄  ▄▄▄▄▄▄▄ ▄▄▄▄▄▄▄\n"
    "▀███  ███  ███▀  ███  █████▀▀▀ ███▀▀███▄\n"
    " ███  ███  ███   ███   ▀████▄  ███▄▄███▀\n"
    " ███▄▄███▄▄███   ███     ▀████ ███▀▀▀▀\n"
    "  ▀████▀████▀   ▄███▄ ███████▀ ███"
)
_TAGLINE = "a quiet coding agent"

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

    .message--banner {
        border-left: none;
        margin: 1 0 0 0;
        padding-left: 1;
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
        border: none;
        border-bottom: heavy $surface-lighten-2;
        padding: 0 1;
    }

    #input:focus {
        border: none;
        border-bottom: heavy $accent;
    }
    """

    # priority=True so these fire even while the Input widget has focus;
    # otherwise Input swallows ctrl+d before it reaches the app bindings.
    #
    # Scrollback: the transcript is not in the focused Input's ancestor chain, so
    # its own scroll bindings never fire — forward the keys from the app instead.
    # home/end are priority=True to beat Input's cursor-jump bindings (ctrl+a /
    # ctrl+e still move the cursor to line start/end); pageup/pagedown have no
    # competing Input binding, so they bubble normally.
    #
    # Copy is handled by the terminal, not the app: the shell runs with mouse
    # reporting off (see run_shell), so drag-select + the OS copy shortcut work
    # natively. That leaves ctrl+c with its traditional interrupt meaning.
    BINDINGS = [
        Binding("ctrl+c", "interrupt", "Interrupt", priority=True),
        Binding("ctrl+d", "eof", "EOF", priority=True),
        Binding("pageup", "scroll_transcript_page_up", "Scroll up", show=False),
        Binding("pagedown", "scroll_transcript_page_down", "Scroll down", show=False),
        Binding("home", "scroll_transcript_home", "Scroll to top", priority=True, show=False),
        Binding("end", "scroll_transcript_end", "Scroll to bottom", priority=True, show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._prompt_send, self._prompt_receive = anyio.create_memory_object_stream[
            str | BaseException
        ](100)
        self._status: Static | None = None
        self._transcript: Transcript | None = None
        self._input: Input | None = None
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
        self._working_widget: WorkingMessage | None = None
        # call_id → ToolCard, so the request, approval, and result events for one
        # tool call all mutate the same card instead of stacking three lines.
        self._tool_cards: dict[str, ToolCard] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            # Transcript takes all remaining height (1fr). The input and compact
            # footer hug the bottom, matching Pi's editor-above-footer visual shape.
            # The input is yielded directly — a wrapping Container would default to
            # height: 1fr and float the input into the middle of the screen.
            yield Transcript(id="transcript")
            # The slash-command menu floats on the overlay layer anchored near the
            # input; yielded here so it shares the Vertical's coordinate space.
            yield SlashSuggest(id="suggest")
            yield DecisionPanel(id="decision-panel")
            yield Input(placeholder=_input_placeholder("wisp> "), id="input")
            with Horizontal(id="status-bar"):
                # markup=False: the footer is always plain data (cwd, session,
                # provider/model, status) — never intentional markup. Static
                # renders markup by default, so a cwd or model name containing
                # bracket syntax (e.g. a dir named `[x]`, or `/model [bold]`)
                # would be interpreted as style tags and could restyle/hide/raise.
                # Disabling markup on the widget makes the footer literal for every
                # set_status() call without escaping at each site.
                yield Static("idle", id="status", markup=False)

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
        self._status = self.query_one("#status", Static)
        self._input = self.query_one("#input", Input)
        self._suggest = self.query_one("#suggest", SlashSuggest)
        self._decision_panel = self.query_one("#decision-panel", DecisionPanel)
        self._input.focus()  # keep the Input as the resting focus
        if self._runner is not None:
            self.run_worker(self._run_and_exit(), exclusive=True)

    def watch_theme(self, theme_name: str) -> None:
        # `theme` is a Textual reactive; re-derive transcript role colors so
        # message widgets mounted after a switch track the new palette. Already-
        # mounted LineMessage widgets keep their baked-in markup colors.
        if self.is_running:
            self._role_styles = role_styles(self.current_theme)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter runs the line as-is through the typed path; the menu (if open) is
        # just a hint, so close it. `/command` parsing lives in the shell.
        if self._suggest is not None:
            self._suggest.hide()
        self.submit_command_line(event.value)

    def on_input_changed(self, event: Input.Changed) -> None:
        # Keep the inline slash menu in sync with the input WITHOUT ever touching
        # the input value — the line is the single source of truth (Claude-Code
        # model), so a leading `/` is always typable as text (`/etc/hosts`). The
        # menu shows only while the value is a bare slash token still being typed;
        # show_for() hides it otherwise (no `/`, a space, or no match).
        if event.input is self._input and self._suggest is not None:
            self._suggest.show_for(event.value)

    def submit_command_line(self, text: str) -> None:
        """Submit a line as if the user typed it and pressed Enter.

        The single entry point for both a real Input submission and a command
        palette selection, so `/command` semantics stay sourced only from the
        shell's typed-line handling. Clears the Input, fires the submit hook (so
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
        """Put a command prefix in the Input, cursor at the end, without submitting.

        Tab-completion fills the highlighted command here (`/model `, `/help`); the
        user then adds any argument and presses Enter through the normal typed path.
        """
        if self._input is not None:
            self._input.value = prefix
            self._input.cursor_position = len(prefix)
            self._input.focus()

    async def on_key(self, event: events.Key) -> None:
        # Menu-scoped keys, handled only while the slash menu is open so normal
        # input (Tab focus, Escape, arrows in the Input) is untouched otherwise.
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
        # mouse=False stops Textual from putting the terminal into mouse-reporting
        # mode (the `?1000h`/`?1003h` sequences). With reporting off, the terminal
        # emulator keeps ownership of click-drag, so selecting text and the OS copy
        # shortcut (Cmd+C on macOS, right-click-copy elsewhere) work natively —
        # exactly as in any other terminal program. The trade-off is that no mouse
        # events reach the app: no wheel-scroll of the transcript (PageUp/PageDown/
        # Home/End cover that) and no click-to-focus (the Input is the resting focus
        # and the palette opens via `/` or ctrl+p). We accept that to make copy
        # behave the way users expect from a terminal.
        await self.run_async(mouse=False)
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

    def action_interrupt(self) -> None:
        self._signal_input(KeyboardInterrupt(), action="interrupt")

    def action_eof(self) -> None:
        self._signal_input(EOFError(), action="EOF")

    # Scrollback: delegate to the Transcript's own scroll actions. The Transcript
    # keeps its follow-the-tail flag correct via watch_scroll_y — scrolling up
    # clears it, scrolling to the end restores it — so we never touch _follow here.
    # None-guarded like _mount_line for calls before on_mount wires the widget.
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
            self._transcript.action_scroll_end()

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

    def set_status(self, message: str) -> None:
        if self._status is not None:
            self._status.update(message)

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

    def show_working_indicator(self) -> None:
        if self._transcript is None or self._working_widget is not None:
            return
        self._working_widget = WorkingMessage()
        self._transcript.mount(self._working_widget)
        self._follow_tail_after_refresh()

    def hide_working_indicator(self) -> None:
        widget = self._working_widget
        self._working_widget = None
        if widget is not None:
            widget.remove()

    def mount_tool_call(self, call_id: str, name: str, arguments: object) -> None:
        # Mount a fresh card for a tool call and register it by call_id. The
        # working indicator is retired: this card now carries the "in progress"
        # signal (pending glyph + dim rule) for the rest of the call's lifecycle.
        if self._transcript is None:
            return
        self.hide_working_indicator()
        card = ToolCard(name, arguments)
        self._tool_cards[call_id] = card
        self._transcript.mount(card)
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
        self._tool_cards.clear()

    def _style(self, role: str) -> str:
        # Resolve a transcript role to a theme-derived Rich style. Falls back to
        # a plain color name if called before on_mount populates the map.
        return self._role_styles.get(role, _ROLE_FALLBACK.get(role, ""))

    def _write_styled(self, role: str, message: str) -> None:
        style = self._style(role)
        escaped = _markup_escape(message)
        self._mount_line(role, f"[{style}]{escaped}[/{style}]" if style else escaped)

    def write_banner(self, art: str) -> None:
        # The startup wordmark: accent-colored, borderless (the "banner" role has
        # no card chrome), rendered verbatim. Block glyphs aren't markup-special,
        # but escape anyway to keep the escape-at-boundary invariant uniform.
        style = self._style("banner")
        escaped = _markup_escape(art)
        self._mount_line("banner", f"[{style}]{escaped}[/{style}]" if style else escaped)

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
        self._transcript.mount(LineMessage(markup, role=role))
        self._follow_tail_after_refresh()

    def append_stream(self, delta: str) -> None:
        # Accumulate into the authoritative buffer; lazily mount the streaming
        # assistant widget on the first delta; reconcile via one coalesced
        # refresh so the Markdown reparses at most once per frame, not per token.
        self.hide_working_indicator()
        self._streaming_text += delta
        if self._stream_widget is None and self._transcript is not None:
            self._stream_widget = StreamMessage()
            self._transcript.mount(self._stream_widget)
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
        await self._follow_tail_after_content(widget.set_content(text))

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
                self._stream_widget.set_content(self._streaming_text)
            )

    async def _follow_tail_after_content(self, await_content: Awaitable[None]) -> None:
        # Await the Markdown update's AwaitComplete so this update's block children
        # have mounted, THEN follow the tail — the scroll lands on the grown extent
        # instead of a partially-mounted one. This replaces guessing a fixed number
        # of refresh cycles with the update's own completion signal. The Transcript
        # still decides whether to scroll (it stays put if the user scrolled away).
        await await_content
        if self._transcript is not None:
            self._transcript.follow_tail()

    def _follow_tail_after_refresh(self) -> None:
        # Non-streamed lines (LineMessage) mount synchronously enough that one
        # post-refresh pass reaches the settled scroll range; used by _mount_line.
        if self._transcript is not None:
            self.call_after_refresh(self._transcript.follow_tail)


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

    def view_updated(self, snapshot: TuiViewSnapshot) -> None:
        self._visible_input_mode = snapshot.input_mode
        self._visible_cwd = snapshot.cwd
        self.app.set_input_hint(snapshot.input_hint)
        # Size the footer to the #status content region, not the app width — the
        # status bar's horizontal padding makes the render area narrower, so app
        # width would over-pad and wrap/clip the two-line footer.
        self.app.set_status(format_tui_footer_text(snapshot, width=self.app.status_width()))
        if snapshot.input_mode != "running":
            self.app.hide_working_indicator()
        if snapshot.input_mode not in {"approval", "trust"}:
            self.app.hide_decision()

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
        # One tight greeting under the wordmark: identity + the two things worth
        # knowing on first launch (the `/` command door, how to leave). Keeping it
        # to a single dim line lets the wordmark breathe without a wall of hints.
        self.app.write_banner(_WORDMARK)
        self.app.write_dim(f"{_TAGLINE} · press / for commands · /quit to exit")

    def help(self) -> None:
        self.app.write_notice(_tui_help_text())

    def notice(self, message: str) -> None:
        self.app.write_notice(message)

    def command_error(self, message: str) -> None:
        self.app.write_error(message)

    def prompt_submitted(self, prompt: str) -> None:
        self.app.write_user(prompt)

    def running(self) -> None:
        self.app.show_working_indicator()

    def queued_follow_up(self, count: int) -> None:
        self.app.write_dim(f"queued follow-up #{count}")

    def running_queued_follow_up(self, count: int) -> None:
        self.app.write_dim(f"running queued follow-up; {count} queued")

    def input_closed_finishing_prompt(self) -> None:
        self.app.write_dim("input closed; finishing current prompt")

    def input_cleared(self) -> None:
        self.app.write_dim("input cleared")

    def cancelling(self, message: str) -> None:
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
        self.app.hide_working_indicator()
        self.app.hide_decision()
        self._abort_pending_tools(f"send failed: {action}")
        self.app.write_error(f"failed to send {action}: {error}")

    def shutdown_failed(self, error: object) -> None:
        self.app.hide_working_indicator()
        self._abort_pending_tools("shutdown failed")
        self.app.write_error(f"shutdown failed: {error}")

    def cancelled(self) -> None:
        self.app.hide_working_indicator()
        self._abort_pending_tools("cancelled")
        self.app.write_notice("cancelled")

    def token_delta(self, delta: str) -> None:
        # Stream live into the assistant Markdown widget; append_stream buffers
        # and coalesces the reconcile. end_token_stream() finalizes the bubble.
        self.app.append_stream(delta)

    def end_token_stream(self) -> None:
        self.app.flush_stream()

    def approval_request(self, event: ToolApprovalRequested) -> None:
        self.app.hide_working_indicator()
        self.app.show_approval(event, cwd=self._visible_cwd)

    def approval_all_confirmation(self, event: ToolApprovalRequested) -> None:
        self.app.show_approval_all_confirmation(event)

    def trust_request(self, event: TrustRequested) -> None:
        self.app.hide_working_indicator()
        self.app.show_trust(event)

    def event(self, event: KnownWispEvent) -> None:
        # Typed dispatch mirroring LineTuiRenderer.event() so tool calls, tool
        # results, and approvals render as distinct, semantically-styled lines
        # instead of an undifferentiated str(event) repr.
        if isinstance(event, MessageCompleted) and event.content:
            self.app.hide_working_indicator()
            self.app.write_assistant(event.content)
        elif isinstance(event, ToolCallRequested):
            # Mount the evolving card; approval/result mutate it in place. Record
            # the request time so the card can show its true duration on resolve.
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
                detail=_first_line(event.output),
                elapsed=self._tool_elapsed(event.call_id, event.timestamp),
            )
        elif isinstance(event, ErrorEvent):
            self.app.hide_working_indicator()
            self.app.write_error(f"error: {event.message}")
        elif isinstance(event, RpcCommandFinished) and not event.ok:
            self.app.hide_working_indicator()
            self._abort_pending_tools("command failed")
            self.app.write_error(f"command failed: {event.error or event.command_id}")
        # Framing/plumbing events (RpcCommandStarted, a successful RpcCommandFinished,
        # AgentStarted, ToolExecutionStarted/Ended, SessionSaved) are intentionally
        # not rendered. They are session/RPC audit, not conversation — and the active
        # session id already lives in the footer, so a per-turn "session saved:"
        # line is pure redundancy. Dropping them keeps the transcript conversational.

    def rpc_event_reader_failed(self, error: str) -> None:
        self.app.hide_working_indicator()
        self._abort_pending_tools("event reader failed")
        self.app.write_error(f"RPC event reader failed: {error}")

    def rpc_stream_ended_before_command(self, command_id: str) -> None:
        self.app.hide_working_indicator()
        self._abort_pending_tools("stream ended")
        self.app.write_error(f"RPC stream ended before command finished: {command_id}")

    def rpc_stream_ended_before_shutdown(self, command_id: str) -> None:
        self.app.hide_working_indicator()
        self._abort_pending_tools("stream ended")
        self.app.write_error(f"RPC stream ended before shutdown finished: {command_id}")

    def rpc_stream_ended_unexpectedly(self) -> None:
        self.app.hide_working_indicator()
        self._abort_pending_tools("stream ended")
        self.app.write_error("RPC stream ended unexpectedly")


def create_textual_tui() -> tuple[TextualTui, TuiRenderer]:
    """Create a Textual app and renderer pair for `TuiShell`."""

    app = TextualTui()
    return app, TextualTuiRenderer(app)


__all__ = ["TextualTui", "TextualTuiRenderer", "create_textual_tui"]
