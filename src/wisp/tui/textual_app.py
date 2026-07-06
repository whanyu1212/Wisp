"""Textual-based fullscreen TUI adapter for Wisp."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import anyio
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.widgets import Footer, Header, Input, RichLog, Static

from wisp.events import KnownWispEvent, ToolApprovalRequested
from wisp.tui.rendering import TuiRenderer, TuiViewSnapshot, _markup_escape, _tui_help_text


class TextualTui(App[None]):
    """Minimal Textual shell that adapts Wisp's existing TUI loop."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #transcript {
        height: 1fr;
        border: round $primary;
    }

    #status {
        height: auto;
        padding: 0 1;
        background: $surface;
        color: $text;
    }

    #input {
        height: auto;
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
        self._rich_log: RichLog | None = None
        self._input: Input | None = None
        self._current_prompt = "wisp> "
        self._runner: Callable[[], Awaitable[None]] | None = None
        self._runner_error: Exception | None = None
        self._streaming_text = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield RichLog(id="transcript", wrap=True, markup=True)
            yield Static("idle", id="status")
            with Container(id="input-row"):
                yield Input(placeholder="wisp> ", id="input")
        yield Footer()

    async def on_mount(self) -> None:
        self._rich_log = self.query_one("#transcript", RichLog)
        self._status = self.query_one("#status", Static)
        self._input = self.query_one("#input", Input)
        self._input.focus()
        if self._runner is not None:
            self.run_worker(self._run_and_exit(), exclusive=True)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._input is not None:
            self._input.value = ""
        await self._prompt_send.send(event.value)

    async def read_prompt(self, prompt: str) -> str:
        self._current_prompt = prompt
        if self._input is not None:
            self._input.placeholder = prompt
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

    def set_status(self, message: str) -> None:
        if self._status is not None:
            self._status.update(message)

    def write_notice(self, message: str) -> None:
        self._write(f"[cyan]{_markup_escape(message)}[/cyan]")

    def write_error(self, message: str) -> None:
        self._write(f"[red]{_markup_escape(message)}[/red]")

    def write_dim(self, message: str) -> None:
        self._write(f"[dim]{_markup_escape(message)}[/dim]")

    def write_user(self, message: str) -> None:
        self._write(f"[bold magenta]you:[/bold magenta] {_markup_escape(message)}")

    def write_assistant(self, message: str) -> None:
        self._write(f"[bold green]assistant:[/bold green] {_markup_escape(message)}")

    def write_event(self, message: str) -> None:
        self._write(_markup_escape(message))

    def append_stream(self, delta: str) -> None:
        self._streaming_text += delta

    def flush_stream(self) -> None:
        if not self._streaming_text:
            return
        streamed = self._streaming_text
        self._streaming_text = ""
        self.write_assistant(streamed)

    def _write(self, message: str) -> None:
        if self._rich_log is not None:
            self._rich_log.write(message)


class TextualTuiRenderer:
    """Renderer adapter consumed by `TuiShell` and backed by `TextualTui`."""

    def __init__(self, app: TextualTui) -> None:
        self.app = app

    def view_updated(self, snapshot: TuiViewSnapshot) -> None:
        parts = [snapshot.status]
        if snapshot.queued_follow_ups:
            parts.append(f"queued: {snapshot.queued_follow_ups}")
        if snapshot.last_session:
            parts.append(f"session: {snapshot.last_session}")
        self.app.set_status(" | ".join(parts))

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
        # RichLog is append-only, so coalesce streamed tokens into one buffer
        # and flush a single "assistant:" line on end_token_stream().
        self.app.append_stream(delta)

    def end_token_stream(self) -> None:
        self.app.flush_stream()

    def approval_request(self, event: ToolApprovalRequested) -> None:
        self.app.write_notice(
            f"? approval required {event.name} ({event.safety}) {event.arguments}"
        )

    def event(self, event: KnownWispEvent) -> None:
        content = getattr(event, "content", None)
        if isinstance(content, str):
            self.app.write_assistant(content)
            return
        message = getattr(event, "message", None)
        if isinstance(message, str):
            self.app.write_error(message)
            return
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
