"""Rendering abstractions for Wisp's terminal UI."""

from __future__ import annotations

import os
from typing import Protocol

from rich.console import Console
from rich.markup import escape

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


class TuiRenderer(Protocol):
    """Renderer surface consumed by the TUI controller loop."""

    def startup(self) -> None: ...

    def help(self) -> None: ...

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


def _compact_session_path(path: object) -> str:
    path_text = str(path)
    return os.path.basename(path_text) or path_text


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "(no output)")


def _markup_escape(value: object) -> str:
    return escape(str(value))
