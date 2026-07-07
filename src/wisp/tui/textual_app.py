"""Textual-based fullscreen TUI adapter for Wisp."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import anyio
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Footer, Header, Input, LoadingIndicator, Static

from wisp.events import (
    AssistantMessage,
    ErrorEvent,
    KnownWispEvent,
    RpcCommandFinished,
    SessionSaved,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolResultReady,
)
from wisp.tui.rendering import (
    TuiRenderer,
    TuiViewSnapshot,
    _compact_session_path,
    _first_line,
    _markup_escape,
    _tui_help_text,
)
from wisp.tui.theme import WISP_THEMES, role_styles
from wisp.tui.widgets import LineMessage, StreamMessage, Transcript

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

# Input modes (TuiViewSnapshot.input_mode values, from state._InputMode) during
# which the activity spinner is shown. Running-only: the approval prompt gets its
# own input hint instead. Kept as the single source of truth for the spinner.
_BUSY_MODES = frozenset({"running"})


class TextualTui(App[None]):
    """Minimal Textual shell that adapts Wisp's existing TUI loop."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #transcript {
        height: 1fr;
        border: round $accent;
    }

    /* Message cards: an L-spine (top + left border) carries the role label on
       the top edge; quiet meta rows stay borderless. Colors come only from
       theme vars present in BOTH light and dark themes. */
    .message {
        height: auto;
        margin: 0 1;
        padding: 0 1;
        border-title-color: $text-muted;
        border-title-style: bold;
    }

    .message--user {
        border-top: hkey $primary;
        border-left: thick $primary;
        background: $primary 8%;
    }

    .message--assistant {
        border-top: hkey $success;
        border-left: thick $success;
        background: $panel;
    }

    .message--tool {
        border-top: hkey $accent;
        border-left: thick $accent;
        background: $surface;
    }

    .message--approved {
        border-left: thick $success;
    }

    .message--denied,
    .message--error {
        border-top: hkey $error;
        border-left: thick $error;
        background: $error 8%;
    }

    .message--notice {
        border-left: thick $accent;
    }

    .message--dim,
    .message--session {
        border-left: none;
        background: transparent;
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
        height: auto;
    }

    #activity {
        width: auto;
        height: 1;
        color: $accent;
    }

    #input {
        height: auto;
        border: tall $surface;
    }

    #input:focus {
        border: tall $accent;
    }
    """

    # priority=True so these fire even while the Input widget has focus;
    # otherwise Input swallows ctrl+d before it reaches the app bindings.
    BINDINGS = [
        Binding("ctrl+c", "interrupt", "Interrupt", priority=True),
        Binding("ctrl+d", "eof", "EOF", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._prompt_send, self._prompt_receive = anyio.create_memory_object_stream[
            str | BaseException
        ](100)
        self._status: Static | None = None
        self._activity: LoadingIndicator | None = None
        self._transcript: Transcript | None = None
        self._input: Input | None = None
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

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield Transcript(id="transcript")
            with Horizontal(id="status-bar"):
                yield Static("idle", id="status")
                yield LoadingIndicator(id="activity")
            with Container(id="input-row"):
                yield Input(placeholder="wisp> ", id="input")
        yield Footer()

    async def on_mount(self) -> None:
        for theme in WISP_THEMES:
            self.register_theme(theme)
        self.theme = WISP_THEMES[0].name
        self._role_styles = role_styles(self.current_theme)
        self._transcript = self.query_one("#transcript", Transcript)
        self._status = self.query_one("#status", Static)
        self._activity = self.query_one("#activity", LoadingIndicator)
        self._activity.display = False  # hidden until a prompt runs
        self._input = self.query_one("#input", Input)
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
        if self._input is not None:
            self._input.value = ""
        if self._on_submit is not None:
            self._on_submit()
        await self._prompt_send.send(event.value)

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
        await self.run_async()
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

    def set_running(self, running: bool) -> None:
        # Toggle the activity spinner's visibility (display, never mount/unmount —
        # avoids the mount race). Driven off the snapshot's input_mode.
        if self._activity is not None:
            self._activity.display = running

    def set_input_hint(self, hint: str) -> None:
        self._current_prompt = hint
        if self._input is not None:
            self._input.placeholder = hint

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

    def write_event(self, message: str) -> None:
        self._mount_line("dim", _markup_escape(message))

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
        self._streaming_text += delta
        if self._stream_widget is None and self._transcript is not None:
            self._stream_widget = StreamMessage()
            self._transcript.mount(self._stream_widget)
        self._schedule_stream_refresh()

    def flush_stream(self) -> None:
        # Finalize the streamed turn. This is the ONLY place a streamed assistant
        # bubble is completed: the shell suppresses the trailing AssistantMessage
        # when tokens were rendered (shell.py de-dup), so it never reaches event().
        # Capture the widget + final text and reconcile AFTER refresh, because the
        # widget may have been mounted this same tick — reconciling inline would
        # hit the mount race and drop the content.
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
        # Mode captured at the instant a line was submitted. It can differ from
        # the mode the shell polled when read_prompt() began waiting (e.g. a
        # tool approval that arrived mid-line), so the shell reconciles against
        # it via consume_submitted_input_mode().
        self._submitted_input_mode: str | None = None
        app.set_submit_hook(self._capture_submitted_input_mode)

    def view_updated(self, snapshot: TuiViewSnapshot) -> None:
        self._visible_input_mode = snapshot.input_mode
        self.app.set_input_hint(snapshot.input_hint)
        parts = [snapshot.status]
        if snapshot.queued_follow_ups:
            parts.append(f"queued: {snapshot.queued_follow_ups}")
        if snapshot.last_session:
            parts.append(f"session: {snapshot.last_session}")
        self.app.set_status(" | ".join(parts))
        self.app.set_running(snapshot.input_mode in _BUSY_MODES)

    def _capture_submitted_input_mode(self) -> None:
        self._submitted_input_mode = self._visible_input_mode

    def consume_submitted_input_mode(self, fallback: str) -> str:
        """Return and clear the mode captured when the last line was accepted."""

        mode = self._submitted_input_mode or fallback
        self._submitted_input_mode = None
        return mode

    def startup(self) -> None:
        self.app.write_notice("Wisp TUI")
        self.app.write_notice("Use /help for commands, /quit to exit.")

    def help(self) -> None:
        self.app.write_notice(_tui_help_text())

    def notice(self, message: str) -> None:
        self.app.write_notice(message)

    def command_error(self, message: str) -> None:
        self.app.write_error(message)

    def prompt_submitted(self, prompt: str) -> None:
        self.app.write_user(prompt)

    def running(self) -> None:
        self.app.write_dim("running...")

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
        self.app.write_error(f"failed to send {action}: {error}")

    def shutdown_failed(self, error: object) -> None:
        self.app.write_error(f"shutdown failed: {error}")

    def cancelled(self) -> None:
        self.app.write_notice("cancelled")

    def token_delta(self, delta: str) -> None:
        # Stream live into the assistant Markdown widget; append_stream buffers
        # and coalesces the reconcile. end_token_stream() finalizes the bubble.
        self.app.append_stream(delta)

    def end_token_stream(self) -> None:
        self.app.flush_stream()

    def approval_request(self, event: ToolApprovalRequested) -> None:
        self.app.write_notice(
            f"? approval required {event.name} ({event.safety}) {event.arguments}"
        )

    def event(self, event: KnownWispEvent) -> None:
        # Typed dispatch mirroring LineTuiRenderer.event() so tool calls, tool
        # results, and approvals render as distinct, semantically-styled lines
        # instead of an undifferentiated str(event) repr.
        if isinstance(event, AssistantMessage):
            self.app.write_assistant(event.content)
        elif isinstance(event, ToolCallRequested):
            self.app.write_labeled("→ tool", f"{event.name} {event.arguments}", role="tool")
        elif isinstance(event, ToolApprovalResolved):
            if event.approved:
                self.app.write_labeled("✓ approved", event.name, role="approved")
            else:
                suffix = f"{event.name}: {event.reason}" if event.reason else event.name
                self.app.write_labeled("! denied", suffix, role="denied")
        elif isinstance(event, ToolResultReady):
            label = "✗ tool" if event.is_error else "✓ tool"
            role = "denied" if event.is_error else "approved"
            self.app.write_labeled(label, f"{event.name}: {_first_line(event.output)}", role=role)
        elif isinstance(event, ErrorEvent):
            self.app.write_error(f"error: {event.message}")
        elif isinstance(event, SessionSaved):
            self.app.write_dim(f"session saved: {_compact_session_path(event.path)}")
        elif isinstance(event, RpcCommandFinished) and not event.ok:
            self.app.write_error(f"command failed: {event.error or event.command_id}")
        else:
            self.app.write_event(str(event))

    def rpc_event_reader_failed(self, error: str) -> None:
        self.app.write_error(f"RPC event reader failed: {error}")

    def rpc_stream_ended_before_command(self, command_id: str) -> None:
        self.app.write_error(f"RPC stream ended before command finished: {command_id}")

    def rpc_stream_ended_before_shutdown(self, command_id: str) -> None:
        self.app.write_error(f"RPC stream ended before shutdown finished: {command_id}")

    def rpc_stream_ended_unexpectedly(self) -> None:
        self.app.write_error("RPC stream ended unexpectedly")


def create_textual_tui() -> tuple[TextualTui, TuiRenderer]:
    """Create a Textual app and renderer pair for `TuiShell`."""

    app = TextualTui()
    return app, TextualTuiRenderer(app)


__all__ = ["TextualTui", "TextualTuiRenderer", "create_textual_tui"]
