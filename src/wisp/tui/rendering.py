"""Rendering abstractions for Wisp's terminal UI."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from rich.console import Console
from rich.layout import Layout
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

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


class TuiRendererKind(StrEnum):
    """Built-in TUI renderer implementations."""

    line = "line"
    fullscreen = "fullscreen"


class TuiRenderer(Protocol):
    """Renderer surface consumed by the TUI controller loop."""

    def startup(self) -> None: ...

    def help(self) -> None: ...

    def prompt_submitted(self, prompt: str) -> None: ...

    def running(self) -> None: ...

    def queued_follow_up(self, count: int) -> None: ...

    def running_queued_follow_up(self) -> None: ...

    def input_closed_finishing_prompt(self) -> None: ...

    def input_cleared(self) -> None: ...

    def cancelling(self, message: str) -> None: ...

    def cancel_already_requested(self) -> None: ...

    def approval_input_closed(self) -> None: ...

    def approval_interrupted(self) -> None: ...

    def quit_requested_denying_approval(self) -> None: ...

    def send_failed(self, action: str, error: object) -> None: ...

    def shutdown_failed(self, error: object) -> None: ...

    def cancelled(self) -> None: ...

    def token_delta(self, delta: str) -> None: ...

    def end_token_stream(self) -> None: ...

    def approval_request(self, event: ToolApprovalRequested) -> None: ...

    def event(self, event: KnownWispEvent) -> None: ...

    def rpc_event_reader_failed(self, error: str) -> None: ...

    def rpc_stream_ended_before_command(self, command_id: str) -> None: ...

    def rpc_stream_ended_before_shutdown(self, command_id: str) -> None: ...

    def rpc_stream_ended_unexpectedly(self) -> None: ...


class LineTuiRenderer:
    """Line-oriented Rich renderer for the current TUI MVP."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    def startup(self) -> None:
        self.console.print("[bold cyan]Wisp TUI MVP[/bold cyan]")
        self.console.print("Type a prompt, /help, /quit, Ctrl-C to interrupt, or Ctrl-D to exit.")

    def help(self) -> None:
        self.console.print("Commands:", markup=False)
        self.console.print("  /help        show this help", markup=False)
        self.console.print("  /quit, /exit quit the TUI", markup=False)
        self.console.print(
            "While a prompt is running, submitted input is queued as a follow-up.",
            markup=False,
        )
        self.console.print("Tool approvals prompt with approve? [y/N].", markup=False)

    def prompt_submitted(self, prompt: str) -> None:
        pass

    def running(self) -> None:
        self.console.print("[dim]running...[/dim]")

    def queued_follow_up(self, count: int) -> None:
        self.console.print(f"[dim]queued follow-up #{count}[/dim]")

    def running_queued_follow_up(self) -> None:
        self.console.print("[dim]running queued follow-up[/dim]")

    def input_closed_finishing_prompt(self) -> None:
        self.console.print("[dim]input closed; finishing current prompt[/dim]")

    def input_cleared(self) -> None:
        self.console.print("[dim]input cleared[/dim]")

    def cancelling(self, message: str) -> None:
        self.console.print(f"\n[yellow]{_markup_escape(message)}[/yellow]")

    def cancel_already_requested(self) -> None:
        self.console.print("[dim]cancel already requested[/dim]")

    def approval_input_closed(self) -> None:
        self.console.print("[yellow]Approval input closed; denying tool request.[/yellow]")

    def approval_interrupted(self) -> None:
        self.console.print("\n[yellow]Approval interrupted; denying tool request.[/yellow]")

    def quit_requested_denying_approval(self) -> None:
        self.console.print("[yellow]Quit requested; denying pending tool request.[/yellow]")

    def send_failed(self, action: str, error: object) -> None:
        self.console.print(f"[red]failed to send {action}:[/red] {_markup_escape(error)}")

    def shutdown_failed(self, error: object) -> None:
        self.console.print(f"[red]shutdown failed:[/red] {_markup_escape(error)}")

    def cancelled(self) -> None:
        self.console.print("[yellow]cancelled[/yellow]")

    def token_delta(self, delta: str) -> None:
        self.console.print(delta, end="", markup=False, highlight=False)

    def end_token_stream(self) -> None:
        self.console.print()

    def approval_request(self, event: ToolApprovalRequested) -> None:
        self.console.print(
            "[yellow]? approval required[/yellow] "
            f"{_markup_escape(event.name)} ({_markup_escape(event.safety)}) "
            f"{_markup_escape(event.arguments)}"
        )

    def event(self, event: KnownWispEvent) -> None:
        if isinstance(event, AssistantMessage):
            self.console.print(event.content, markup=False, highlight=False)
        elif isinstance(event, ToolCallRequested):
            self.console.print(
                f"[blue]→ tool[/blue] {_markup_escape(event.name)} "
                f"{_markup_escape(event.arguments)}"
            )
        elif isinstance(event, ToolApprovalResolved):
            if event.approved:
                self.console.print(f"[green]✓ approved[/green] {_markup_escape(event.name)}")
            else:
                reason = f": {_markup_escape(event.reason)}" if event.reason else ""
                self.console.print(f"[red]! denied[/red] {_markup_escape(event.name)}{reason}")
        elif isinstance(event, ToolResultReady):
            status = "[red]✗[/red]" if event.is_error else "[green]✓[/green]"
            tool_name = _markup_escape(event.name)
            output = _markup_escape(_first_line(event.output))
            self.console.print(f"{status} tool {tool_name}: {output}")
        elif isinstance(event, ErrorEvent):
            self.console.print(f"[red]error:[/red] {_markup_escape(event.message)}")
        elif isinstance(event, SessionSaved):
            self.console.print(
                f"[dim]session saved: {_markup_escape(_compact_session_path(event.path))}[/dim]"
            )
        elif isinstance(event, RpcCommandFinished) and not event.ok:
            self.console.print(
                f"[red]command failed:[/red] {_markup_escape(event.error or event.command_id)}"
            )

    def rpc_event_reader_failed(self, error: str) -> None:
        self.console.print(f"[red]RPC event reader failed:[/red] {_markup_escape(error)}")

    def rpc_stream_ended_before_command(self, command_id: str) -> None:
        self.console.print(
            "[red]RPC event stream ended before command completed: "
            f"{_markup_escape(command_id)}[/red]"
        )

    def rpc_stream_ended_before_shutdown(self, command_id: str) -> None:
        self.console.print(
            "[red]RPC event stream ended before shutdown completed: "
            f"{_markup_escape(command_id)}[/red]"
        )

    def rpc_stream_ended_unexpectedly(self) -> None:
        self.console.print("[red]RPC event stream ended unexpectedly.[/red]")


@dataclass(frozen=True)
class TuiTranscriptEntry:
    role: str
    content: str
    style: str = ""


@dataclass
class FullscreenTuiState:
    """Render-state snapshot used by the full-screen layout foundation."""

    status: str = "idle"
    input_hint: str = "wisp> "
    queued_follow_ups: int = 0
    last_session: str | None = None
    transcript: list[TuiTranscriptEntry] = field(default_factory=list)
    streaming_text: str = ""


class FullscreenTuiRenderer:
    """Panel-based full-screen layout foundation for the Wisp TUI.

    The interaction loop is still line-oriented; this renderer establishes the
    transcript/status/input regions that a future richer TUI can make live.
    """

    def __init__(
        self,
        console: Console | None = None,
        *,
        max_transcript_entries: int = 200,
        clear_screen: bool | None = None,
    ) -> None:
        self.console = console or Console()
        self.state = FullscreenTuiState()
        self.max_transcript_entries = max_transcript_entries
        is_terminal = bool(getattr(self.console, "is_terminal", False))
        self.clear_screen = is_terminal if clear_screen is None else clear_screen

    def startup(self) -> None:
        self.state.status = "idle"
        self.state.input_hint = "wisp> "
        self._refresh()

    def help(self) -> None:
        self._append(
            "help",
            "Commands:\n"
            "  /help        show this help\n"
            "  /quit, /exit quit the TUI\n"
            "While a prompt is running, submitted input is queued as a follow-up.\n"
            "Tool approvals prompt with approve? [y/N].",
            style="cyan",
        )
        self._refresh()

    def prompt_submitted(self, prompt: str) -> None:
        self._append("user", prompt, style="bold")

    def running(self) -> None:
        self.state.status = "running"
        self.state.input_hint = "wisp(running)> "
        self._refresh()

    def queued_follow_up(self, count: int) -> None:
        self.state.queued_follow_ups = count
        self._append("system", f"queued follow-up #{count}", style="dim")
        self._refresh()

    def running_queued_follow_up(self) -> None:
        self.state.status = "running queued follow-up"
        self.state.input_hint = "wisp(running)> "
        self._append("system", "running queued follow-up", style="dim")
        self._refresh()

    def input_closed_finishing_prompt(self) -> None:
        self.state.queued_follow_ups = 0
        self._append("system", "input closed; finishing current prompt", style="dim")
        self._refresh()

    def input_cleared(self) -> None:
        self._append("system", "input cleared", style="dim")
        self._refresh()

    def cancelling(self, message: str) -> None:
        self.state.status = "cancelling"
        self._append("system", message, style="yellow")
        self._refresh()

    def cancel_already_requested(self) -> None:
        self._append("system", "cancel already requested", style="dim")
        self._refresh()

    def approval_input_closed(self) -> None:
        self._append("approval", "Approval input closed; denying tool request.", style="yellow")
        self._refresh()

    def approval_interrupted(self) -> None:
        self._append("approval", "Approval interrupted; denying tool request.", style="yellow")
        self._refresh()

    def quit_requested_denying_approval(self) -> None:
        self._append("approval", "Quit requested; denying pending tool request.", style="yellow")
        self._refresh()

    def send_failed(self, action: str, error: object) -> None:
        self.state.status = "error"
        self._append("error", f"failed to send {action}: {error}", style="red")
        self._refresh()

    def shutdown_failed(self, error: object) -> None:
        self.state.status = "error"
        self._append("error", f"shutdown failed: {error}", style="red")
        self._refresh()

    def cancelled(self) -> None:
        self.state.status = "idle"
        self.state.input_hint = "wisp> "
        self.state.queued_follow_ups = 0
        self._append("system", "cancelled", style="yellow")
        self._refresh()

    def token_delta(self, delta: str) -> None:
        self.state.status = "running"
        self.state.streaming_text += delta
        self._refresh()

    def end_token_stream(self) -> None:
        if self.state.streaming_text:
            self._append("assistant", self.state.streaming_text, style="green")
            self.state.streaming_text = ""
        self._refresh()

    def approval_request(self, event: ToolApprovalRequested) -> None:
        self.state.status = "waiting for approval"
        self.state.input_hint = "approve? [y/N] "
        self._append(
            "approval",
            f"? approval required {event.name} ({event.safety}) {event.arguments}",
            style="yellow",
        )
        self._refresh()

    def event(self, event: KnownWispEvent) -> None:
        if isinstance(event, AssistantMessage):
            self._append("assistant", event.content, style="green")
        elif isinstance(event, ToolCallRequested):
            self._append("tool", f"→ tool {event.name} {event.arguments}", style="blue")
        elif isinstance(event, ToolApprovalResolved):
            self.state.status = "running"
            self.state.input_hint = "wisp(running)> "
            if event.approved:
                self._append("approval", f"✓ approved {event.name}", style="green")
            else:
                reason = f": {event.reason}" if event.reason else ""
                self._append("approval", f"! denied {event.name}{reason}", style="red")
        elif isinstance(event, ToolResultReady):
            status = "✗" if event.is_error else "✓"
            self._append(
                "tool", f"{status} tool {event.name}: {_first_line(event.output)}", style="blue"
            )
        elif isinstance(event, ErrorEvent):
            self.state.status = "error"
            self._append("error", f"error: {event.message}", style="red")
        elif isinstance(event, SessionSaved):
            self.state.last_session = _compact_session_path(event.path)
            self._append("session", f"session saved: {self.state.last_session}", style="dim")
        elif isinstance(event, RpcCommandFinished):
            if event.ok and event.command_type in {"prompt", "shutdown"}:
                self.state.status = "idle"
                self.state.input_hint = "wisp> "
                self.state.queued_follow_ups = 0
            elif not event.ok:
                self.state.status = "error"
                if event.command_type == "prompt":
                    self.state.queued_follow_ups = 0
                self._append(
                    "error",
                    f"command failed: {event.error or event.command_id}",
                    style="red",
                )
        self._refresh()

    def rpc_event_reader_failed(self, error: str) -> None:
        self.state.status = "error"
        self._append("error", f"RPC event reader failed: {error}", style="red")
        self._refresh()

    def rpc_stream_ended_before_command(self, command_id: str) -> None:
        self.state.status = "error"
        self._append(
            "error",
            f"RPC event stream ended before command completed: {command_id}",
            style="red",
        )
        self._refresh()

    def rpc_stream_ended_before_shutdown(self, command_id: str) -> None:
        self.state.status = "error"
        self._append(
            "error",
            f"RPC event stream ended before shutdown completed: {command_id}",
            style="red",
        )
        self._refresh()

    def rpc_stream_ended_unexpectedly(self) -> None:
        self.state.status = "error"
        self._append("error", "RPC event stream ended unexpectedly.", style="red")
        self._refresh()

    def _append(self, role: str, content: object, *, style: str = "") -> None:
        self.state.transcript.append(TuiTranscriptEntry(role, str(content), style))
        if len(self.state.transcript) > self.max_transcript_entries:
            excess = len(self.state.transcript) - self.max_transcript_entries
            del self.state.transcript[:excess]

    def _refresh(self) -> None:
        if self.clear_screen:
            self.console.clear()
        self.console.print(self._layout())

    def _layout(self) -> Layout:
        layout = Layout(name="wisp")
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="transcript", ratio=1),
            Layout(name="footer", size=6),
        )
        layout["header"].update(
            Panel(
                Text("Wisp", style="bold cyan"),
                subtitle="RPC-backed TUI",
                border_style="cyan",
            )
        )
        layout["transcript"].update(
            Panel(
                self._transcript_text(),
                title="Transcript",
                border_style="cyan",
            )
        )
        layout["footer"].split_row(Layout(name="status"), Layout(name="input"))
        layout["footer"]["status"].update(
            Panel(self._status_text(), title="Status", border_style="magenta")
        )
        layout["footer"]["input"].update(
            Panel(self._input_text(), title="Input", border_style="green")
        )
        return layout

    def _transcript_text(self) -> Text:
        text = Text()
        if not self.state.transcript and not self.state.streaming_text:
            text.append("No messages yet.", style="dim")
            return text
        for entry in self.state.transcript:
            self._append_entry_text(text, entry)
        if self.state.streaming_text:
            self._append_entry_text(
                text,
                TuiTranscriptEntry("assistant", self.state.streaming_text, "green"),
            )
        return text

    def _append_entry_text(self, text: Text, entry: TuiTranscriptEntry) -> None:
        if text.plain:
            text.append("\n")
        label_style = f"bold {entry.style}" if entry.style else "bold"
        text.append(f"{entry.role}: ", style=label_style)
        text.append(entry.content, style=entry.style)

    def _status_text(self) -> Text:
        text = Text()
        text.append(self.state.status, style="bold")
        if self.state.queued_follow_ups:
            text.append(f"\nqueued follow-ups: {self.state.queued_follow_ups}", style="dim")
        if self.state.last_session:
            text.append(f"\nsession: {self.state.last_session}", style="dim")
        return text

    def _input_text(self) -> Text:
        text = Text(self.state.input_hint)
        text.append(
            "\n/help for commands · /quit to exit · Ctrl-C interrupt · Ctrl-D EOF", style="dim"
        )
        return text


_BUILT_IN_RENDERERS: dict[TuiRendererKind, type[LineTuiRenderer] | type[FullscreenTuiRenderer]] = {
    TuiRendererKind.line: LineTuiRenderer,
    TuiRendererKind.fullscreen: FullscreenTuiRenderer,
}


def create_tui_renderer(kind: TuiRendererKind, console: Console | None = None) -> TuiRenderer:
    """Create a built-in TUI renderer."""

    return _BUILT_IN_RENDERERS[kind](console)


def _compact_session_path(path: object) -> str:
    path_text = str(path)
    return os.path.basename(path_text) or path_text


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "(no output)")


def _markup_escape(value: object) -> str:
    return escape(str(value))
