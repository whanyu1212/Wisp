"""Rendering abstractions for Wisp's terminal UI."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Protocol

from rich.cells import cell_len, set_cell_size
from rich.console import Console
from rich.layout import Layout
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from wisp.events import (
    ErrorEvent,
    KnownWispEvent,
    MessageCompleted,
    ProviderRetrying,
    RpcCommandFinished,
    SessionSaved,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolResultReady,
    TrustRequested,
)
from wisp.providers.catalog import ModelCatalogProviderEntry


class TuiRendererKind(StrEnum):
    """Built-in TUI renderer implementations."""

    line = "line"
    fullscreen = "fullscreen"
    textual = "textual"


@dataclass(frozen=True)
class TuiViewSnapshot:
    """Renderer-facing snapshot of shell-owned TUI view state."""

    status: str
    input_hint: str
    input_mode: str = "idle"
    queued_follow_ups: int = 0
    last_session: str | None = None
    cwd: str = ""
    provider: str | None = None
    model: str | None = None


class TuiRenderer(Protocol):
    """Renderer surface consumed by the TUI controller loop."""

    def view_updated(self, snapshot: TuiViewSnapshot) -> None: ...

    def startup(self) -> None: ...

    def help(self) -> None: ...

    def notice(self, message: str) -> None: ...

    def command_error(self, message: str) -> None: ...

    def prompt_submitted(self, prompt: str) -> None: ...

    def queued_prompts_cleared(self) -> None: ...

    def running(self) -> None: ...

    def queued_follow_up(self, count: int) -> None: ...

    def running_queued_follow_up(self, count: int) -> None: ...

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

    def approval_all_confirmation(self, event: ToolApprovalRequested) -> None: ...

    def trust_request(self, event: TrustRequested) -> None: ...

    def model_picker_request(
        self,
        entries: tuple[ModelCatalogProviderEntry, ...],
        *,
        current_provider: str,
        current_model: str | None,
        current_effort: str | None,
    ) -> None: ...

    def event(self, event: KnownWispEvent) -> None: ...

    def rpc_event_reader_failed(self, error: str) -> None: ...

    def rpc_stream_ended_before_command(self, command_id: str) -> None: ...

    def rpc_stream_ended_before_shutdown(self, command_id: str) -> None: ...

    def rpc_stream_ended_unexpectedly(self) -> None: ...


class LineTuiRenderer:
    """Line-oriented Rich renderer for the current TUI MVP."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    def view_updated(self, snapshot: TuiViewSnapshot) -> None:
        pass

    def startup(self) -> None:
        self.console.print("[bold cyan]Wisp TUI MVP[/bold cyan]")
        self.console.print("Type a prompt, /help, /quit, Ctrl-C to interrupt, or Ctrl-D to exit.")

    def help(self) -> None:
        self.console.print(_tui_help_text(), markup=False)

    def notice(self, message: str) -> None:
        self.console.print(f"[cyan]{_markup_escape(message)}[/cyan]")

    def command_error(self, message: str) -> None:
        self.console.print(f"[red]{_markup_escape(message)}[/red]")

    def prompt_submitted(self, prompt: str) -> None:
        pass

    def queued_prompts_cleared(self) -> None:
        # No large-paste compact-echo cache in the text renderer; nothing to drop.
        pass

    def running(self) -> None:
        self.console.print("[dim]running...[/dim]")

    def queued_follow_up(self, count: int) -> None:
        self.console.print(f"[dim]queued follow-up #{count}[/dim]")

    def running_queued_follow_up(self, count: int) -> None:
        self.console.print(f"[dim]running queued follow-up; {count} queued[/dim]")

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

    def approval_all_confirmation(self, event: ToolApprovalRequested) -> None:
        self.console.print(
            "[bold yellow]Enable YOLO for this TUI run?[/bold yellow]\n"
            "All mutating and command tools will run without further approval "
            "until this Wisp process exits."
        )

    def trust_request(self, event: TrustRequested) -> None:
        self.console.print(
            "[yellow]? trust this project?[/yellow] "
            f"{_markup_escape(event.project_path)}\n"
            "Trusting lets Wisp load this project's local configuration."
        )

    def model_picker_request(
        self,
        entries: tuple[ModelCatalogProviderEntry, ...],
        *,
        current_provider: str,
        current_model: str | None,
        current_effort: str | None,
    ) -> None:
        # No interactive picker outside the Textual renderer -- falls back to
        # the same grouped listing bare `/model` already printed before the
        # picker existed. Use `/model <id> [effort]` to switch.
        self.console.print(
            _render_model_listing_text(
                entries,
                current_provider=current_provider,
                current_model=current_model,
                current_effort=current_effort,
            )
        )

    def event(self, event: KnownWispEvent) -> None:
        if isinstance(event, ProviderRetrying):
            status = f" ({event.status_code})" if event.status_code is not None else ""
            self.console.print(
                f"[dim]retrying {event.provider}: {event.reason}{status}; "
                f"attempt {event.attempt}/{event.max_attempts} in {event.delay_seconds:.1f}s[/dim]"
            )
        elif isinstance(event, MessageCompleted) and event.content:
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


@dataclass(frozen=True)
class _RenderedTranscriptLine:
    role: str
    content: str
    style: str = ""


@dataclass
class FullscreenTuiState:
    """Render-state snapshot used by the full-screen layout foundation."""

    status: str = "idle"
    input_hint: str = "wisp> "
    input_mode: str = "idle"
    queued_follow_ups: int = 0
    last_session: str | None = None
    cwd: str = ""
    provider: str | None = None
    model: str | None = None
    transcript: list[TuiTranscriptEntry] = field(default_factory=list)
    streaming_text: str = ""
    transcript_scroll_offset: int = 0
    transcript_view_entries: int = 50


class FullscreenTuiRenderer:
    """Panel-based full-screen layout foundation for the Wisp TUI.

    The interaction loop is still line-oriented; this renderer establishes the
    transcript/editor/footer regions that a future richer TUI can make live.
    """

    def __init__(
        self,
        console: Console | None = None,
        *,
        max_transcript_entries: int = 200,
        transcript_view_entries: int = 50,
        clear_screen: bool | None = None,
    ) -> None:
        self.console = console or Console()
        self.state = FullscreenTuiState(
            transcript_view_entries=max(1, transcript_view_entries),
        )
        self.max_transcript_entries = max_transcript_entries
        # Input is still line-oriented via input(), so clearing a real terminal
        # during background RPC refreshes would erase the active input line and
        # any partially typed follow-up. Keep clearing opt-in until input is
        # owned by the renderer/live full-screen UI.
        self.clear_screen = False if clear_screen is None else clear_screen

    def view_updated(self, snapshot: TuiViewSnapshot) -> None:
        self.state.status = snapshot.status
        self.state.input_hint = snapshot.input_hint
        self.state.input_mode = snapshot.input_mode
        self.state.queued_follow_ups = snapshot.queued_follow_ups
        self.state.last_session = snapshot.last_session
        self.state.cwd = snapshot.cwd
        self.state.provider = snapshot.provider
        self.state.model = snapshot.model
        self._refresh()

    def startup(self) -> None:
        self._refresh()

    def help(self) -> None:
        self._append("help", _tui_help_text(), style="cyan")
        self._refresh()

    def notice(self, message: str) -> None:
        self._append("system", message, style="cyan")
        self._refresh()

    def command_error(self, message: str) -> None:
        self._append("error", message, style="red")
        self._refresh()

    def prompt_submitted(self, prompt: str) -> None:
        self._append("user", prompt, style="bold")

    def queued_prompts_cleared(self) -> None:
        # No large-paste compact-echo cache in the text renderer; nothing to drop.
        pass

    def running(self) -> None:
        self._refresh()

    def queued_follow_up(self, count: int) -> None:
        self._append("system", f"queued follow-up #{count}", style="dim")
        self._refresh()

    def running_queued_follow_up(self, count: int) -> None:
        self._append("system", "running queued follow-up", style="dim")
        self._refresh()

    def input_closed_finishing_prompt(self) -> None:
        self._append("system", "input closed; finishing current prompt", style="dim")
        self._refresh()

    def input_cleared(self) -> None:
        self._append("system", "input cleared", style="dim")
        self._refresh()

    def cancelling(self, message: str) -> None:
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
        self._append("error", f"failed to send {action}: {error}", style="red")
        self._refresh()

    def shutdown_failed(self, error: object) -> None:
        self._append("error", f"shutdown failed: {error}", style="red")
        self._refresh()

    def cancelled(self) -> None:
        self._append("system", "cancelled", style="yellow")
        self._refresh()

    def token_delta(self, delta: str) -> None:
        previous_lines = len(self._rendered_transcript_lines())
        self.state.streaming_text += delta
        self._preserve_scroll_after_line_count_change(previous_lines)
        # The layout foundation still uses line-oriented input and a plain
        # console renderer. Redrawing the full layout for every token would
        # append repeated frames when clear_screen is disabled, so coalesce
        # streamed text until end_token_stream() can render one updated frame.

    def end_token_stream(self) -> None:
        if self.state.streaming_text:
            streaming_text = self.state.streaming_text
            self.state.streaming_text = ""
            self._append(
                "assistant",
                streaming_text,
                style="green",
                preserve_scroll=False,
            )
            self._clamp_transcript_scroll()
        self._refresh()

    def approval_request(self, event: ToolApprovalRequested) -> None:
        self._append(
            "approval",
            f"? approval required {event.name} ({event.safety}) {event.arguments}",
            style="yellow",
        )
        self._refresh()

    def approval_all_confirmation(self, event: ToolApprovalRequested) -> None:
        self._append(
            "approval",
            "Enable YOLO for this TUI run? All mutating and command tools will run "
            "without further approval until this Wisp process exits.",
            style="bold yellow",
        )
        self._refresh()

    def trust_request(self, event: TrustRequested) -> None:
        self._append(
            "trust",
            f"? trust this project? {event.project_path}",
            style="yellow",
        )
        self._refresh()

    def model_picker_request(
        self,
        entries: tuple[ModelCatalogProviderEntry, ...],
        *,
        current_provider: str,
        current_model: str | None,
        current_effort: str | None,
    ) -> None:
        # No interactive picker outside the Textual renderer -- see
        # LineTuiRenderer.model_picker_request for the same fallback text.
        self._append(
            "system",
            _render_model_listing_text(
                entries,
                current_provider=current_provider,
                current_model=current_model,
                current_effort=current_effort,
            ),
            style="cyan",
        )
        self._refresh()

    def scroll_transcript_up(self, amount: int | None = None) -> None:
        self.state.transcript_scroll_offset += self._scroll_amount(amount)
        self._clamp_transcript_scroll()
        self._refresh()

    def scroll_transcript_down(self, amount: int | None = None) -> None:
        self.state.transcript_scroll_offset -= self._scroll_amount(amount)
        self._clamp_transcript_scroll()
        self._refresh()

    def scroll_transcript_top(self) -> None:
        self.state.transcript_scroll_offset = self._max_transcript_scroll_offset()
        self._refresh()

    def scroll_transcript_bottom(self) -> None:
        self.state.transcript_scroll_offset = 0
        self._refresh()

    def event(self, event: KnownWispEvent) -> None:
        if isinstance(event, MessageCompleted) and event.content:
            self._append("assistant", event.content, style="green")
        elif isinstance(event, ToolCallRequested):
            self._append("tool", f"→ tool {event.name} {event.arguments}", style="blue")
        elif isinstance(event, ToolApprovalResolved):
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
            self._append("error", f"error: {event.message}", style="red")
        elif isinstance(event, SessionSaved):
            self._append(
                "session",
                f"session saved: {_compact_session_path(event.path)}",
                style="dim",
            )
        elif isinstance(event, RpcCommandFinished) and not event.ok:
            self._append(
                "error",
                f"command failed: {event.error or event.command_id}",
                style="red",
            )
        self._refresh()

    def rpc_event_reader_failed(self, error: str) -> None:
        self._append("error", f"RPC event reader failed: {error}", style="red")
        self._refresh()

    def rpc_stream_ended_before_command(self, command_id: str) -> None:
        self._append(
            "error",
            f"RPC event stream ended before command completed: {command_id}",
            style="red",
        )
        self._refresh()

    def rpc_stream_ended_before_shutdown(self, command_id: str) -> None:
        self._append(
            "error",
            f"RPC event stream ended before shutdown completed: {command_id}",
            style="red",
        )
        self._refresh()

    def rpc_stream_ended_unexpectedly(self) -> None:
        self._append("error", "RPC event stream ended unexpectedly.", style="red")
        self._refresh()

    def _append(
        self,
        role: str,
        content: object,
        *,
        style: str = "",
        preserve_scroll: bool = True,
    ) -> None:
        entry = TuiTranscriptEntry(role, str(content), style)
        appended_lines = len(self._rendered_entry_lines(entry))
        self.state.transcript.append(entry)
        if len(self.state.transcript) > self.max_transcript_entries:
            excess = len(self.state.transcript) - self.max_transcript_entries
            del self.state.transcript[:excess]
        if preserve_scroll:
            self._preserve_scroll_after_appended_lines(appended_lines)
        else:
            self._clamp_transcript_scroll()

    def _refresh(self) -> None:
        if self.clear_screen:
            self.console.clear()
        self.console.print(self._layout())

    def _layout(self) -> Layout:
        layout = Layout(name="wisp")
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="transcript", ratio=1),
            Layout(name="editor", size=3),
            Layout(name="footer", size=2),
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
                title=self._transcript_title(),
                border_style="cyan",
            )
        )
        layout["editor"].update(Panel(self._input_text(), title="Editor", border_style="green"))
        layout["footer"].update(self._footer_text())
        return layout

    def _transcript_text(self) -> Text:
        text = Text()
        entries = self._visible_transcript_entries()
        if not entries:
            text.append("No messages yet.", style="dim")
            return text
        for entry in entries:
            self._append_transcript_line_text(text, entry)
        return text

    def _transcript_title(self) -> str:
        max_offset = self._max_transcript_scroll_offset()
        if max_offset <= 0:
            return "Transcript"
        if self.state.transcript_scroll_offset <= 0:
            return "Transcript (latest)"
        return f"Transcript ({self.state.transcript_scroll_offset}/{max_offset})"

    def _transcript_entries(self) -> list[TuiTranscriptEntry]:
        entries = list(self.state.transcript)
        if self.state.streaming_text:
            entries.append(TuiTranscriptEntry("assistant", self.state.streaming_text, "green"))
        return entries

    def _visible_transcript_entries(self) -> list[_RenderedTranscriptLine]:
        entries = self._rendered_transcript_lines()
        if not entries:
            return []
        self._clamp_transcript_scroll()
        visible_count = min(self._transcript_view_entries(), len(entries))
        end = len(entries) - self.state.transcript_scroll_offset
        start = max(0, end - visible_count)
        return entries[start:end]

    def _rendered_transcript_lines(self) -> list[_RenderedTranscriptLine]:
        lines: list[_RenderedTranscriptLine] = []
        for entry in self._transcript_entries():
            lines.extend(self._rendered_entry_lines(entry))
        return lines

    def _rendered_entry_lines(self, entry: TuiTranscriptEntry) -> list[_RenderedTranscriptLine]:
        rendered: list[_RenderedTranscriptLine] = []
        raw_lines = entry.content.splitlines() or [""]
        for line_index, raw_line in enumerate(raw_lines):
            prefix_width = len(f"{entry.role}: ") if line_index == 0 else 0
            wrap_width = self._line_wrap_width(prefix_width=prefix_width)
            for part_index, part in enumerate(_wrap_transcript_line(raw_line, width=wrap_width)):
                rendered.append(
                    _RenderedTranscriptLine(
                        entry.role if line_index == 0 and part_index == 0 else "",
                        part,
                        entry.style,
                    )
                )
        return rendered

    def _line_wrap_width(self, *, prefix_width: int) -> int | None:
        width = self._transcript_wrap_width()
        if width is None:
            return None
        return max(1, width - prefix_width)

    def _transcript_wrap_width(self) -> int | None:
        return None

    def _transcript_view_entries(self) -> int:
        return max(1, self.state.transcript_view_entries)

    def _scroll_amount(self, amount: int | None) -> int:
        if amount is None:
            return max(1, self._transcript_view_entries() - 1)
        return max(1, amount)

    def _max_transcript_scroll_offset(self) -> int:
        return max(0, len(self._rendered_transcript_lines()) - self._transcript_view_entries())

    def _preserve_scroll_after_line_count_change(self, previous_lines: int) -> None:
        self._preserve_scroll_after_appended_lines(
            len(self._rendered_transcript_lines()) - previous_lines
        )

    def _preserve_scroll_after_appended_lines(self, appended_lines: int) -> None:
        if self.state.transcript_scroll_offset > 0:
            self.state.transcript_scroll_offset += max(0, appended_lines)
        self._clamp_transcript_scroll()

    def _clamp_transcript_scroll(self) -> None:
        self.state.transcript_scroll_offset = min(
            max(0, self.state.transcript_scroll_offset),
            self._max_transcript_scroll_offset(),
        )

    def _append_transcript_line_text(self, text: Text, entry: _RenderedTranscriptLine) -> None:
        if text.plain:
            text.append("\n")
        if entry.role:
            label_style = f"bold {entry.style}" if entry.style else "bold"
            text.append(f"{entry.role}: ", style=label_style)
        text.append(entry.content, style=entry.style)

    def _footer_text(self) -> Text:
        lines = format_tui_footer_lines(self._view_snapshot(), width=max(1, self.console.width))
        text = Text()
        for index, line in enumerate(lines):
            if index:
                text.append("\n")
            text.append(line, style="dim")
        return text

    def _view_snapshot(self) -> TuiViewSnapshot:
        return TuiViewSnapshot(
            status=self.state.status,
            input_hint=self.state.input_hint,
            input_mode=self.state.input_mode,
            queued_follow_ups=self.state.queued_follow_ups,
            last_session=self.state.last_session,
            cwd=self.state.cwd,
            provider=self.state.provider,
            model=self.state.model,
        )

    def _input_text(self) -> Text:
        return Text(self.state.input_hint)


def format_tui_footer_lines(
    snapshot: TuiViewSnapshot, *, width: int | None = None
) -> tuple[str, str]:
    """Return Pi-style compact footer lines for a renderer snapshot.

    Field priority (highest to lowest, protected in that order under width
    pressure): status+queued, cwd, provider/model, session id. Both lines
    truncate with ``priority="left"``: line 1's left field is cwd (session
    drops entirely before cwd is ever clipped), line 2's left field is status
    (model clips, or drops entirely, before status is ever clipped).
    """

    display_width = max(1, width) if width is not None else None
    context_left = _sanitize_footer_text(_format_cwd_for_footer(snapshot.cwd))
    context_right = (
        _sanitize_footer_text(f"session: {snapshot.last_session}") if snapshot.last_session else ""
    )

    status_parts = [snapshot.status]
    if snapshot.queued_follow_ups:
        status_parts.append(f"queued {snapshot.queued_follow_ups}")
    status_left = _sanitize_footer_text(" • ".join(status_parts))
    model_right = _sanitize_footer_text(_footer_model_text(snapshot.provider, snapshot.model))

    return (
        _align_footer_line(context_left, context_right, display_width, priority="left"),
        _align_footer_line(status_left, model_right, display_width, priority="left"),
    )


def format_tui_footer_text(snapshot: TuiViewSnapshot, *, width: int | None = None) -> str:
    """Return footer lines joined for renderers that take plain text."""

    return "\n".join(format_tui_footer_lines(snapshot, width=width))


def _format_cwd_for_footer(cwd: str) -> str:
    selected = cwd or os.getcwd()
    expanded = os.path.abspath(os.path.expanduser(selected))
    home = os.path.expanduser("~")
    try:
        if home and os.path.commonpath([expanded, home]) == home:
            relative = os.path.relpath(expanded, home)
            return "~" if relative == "." else f"~{os.sep}{relative}"
    except ValueError:
        return selected
    return selected


def _footer_model_text(provider: str | None, model: str | None) -> str:
    if provider and model:
        return f"{provider}/{model}"
    if provider:
        return f"{provider}/default"
    return model or ""


def _align_footer_line(
    left: str, right: str, width: int | None, *, priority: Literal["left", "right"] = "right"
) -> str:
    """Right-align ``right`` against ``left`` in ``width`` cells.

    ``priority`` names the field protected from truncation when both don't
    fit. ``"right"`` keeps the historical behavior (right field always kept
    whole, left field truncates). ``"left"`` protects the left field instead:
    the right field is dropped entirely — not character-truncated into an
    unreadable fragment — before the left field is ever clipped.
    """

    if not right:
        return _truncate_to_cell_width(left, width)
    if width is None:
        return f"{left}  {right}" if left else right

    left_width = cell_len(left)
    right_width = cell_len(right)
    if left_width + 2 + right_width <= width:
        return left + " " * (width - left_width - right_width) + right

    if priority == "left":
        if left_width >= width:
            return _truncate_to_cell_width(left, width)
        return _align_footer_line(left, "", width, priority="left")

    if not left:
        return _truncate_to_cell_width(right, width)
    if right_width >= width:
        # A protected left field still gets first claim on the width: give it
        # a minimum share rather than letting an oversized right field push
        # it out entirely (the historical bug this priority scheme fixes).
        available_right = width - min(left_width, width // 2) - 2
        if available_right > 0:
            truncated_left = _truncate_to_cell_width(left, width - available_right - 2)
            truncated_right = _truncate_to_cell_width(right, available_right)
            padding = " " * max(1, width - cell_len(truncated_left) - cell_len(truncated_right))
            return truncated_left + padding + truncated_right
        return _truncate_to_cell_width(left, width)

    available_left = width - right_width - 2
    if available_left > 0:
        truncated_left = _truncate_to_cell_width(left, available_left)
        padding = " " * max(1, width - cell_len(truncated_left) - right_width)
        return truncated_left + padding + right
    return _truncate_to_cell_width(right, width)


def _truncate_to_cell_width(text: str, width: int | None, ellipsis: str = "…") -> str:
    if width is None or cell_len(text) <= width:
        return text
    if width <= 0:
        return ""
    ellipsis_width = cell_len(ellipsis)
    if width <= ellipsis_width:
        return set_cell_size(ellipsis, width)
    return set_cell_size(text, width - ellipsis_width) + ellipsis


def _sanitize_footer_text(text: str) -> str:
    return " ".join(text.replace("\r", " ").replace("\n", " ").replace("\t", " ").split())


_BUILT_IN_RENDERERS: dict[TuiRendererKind, type[LineTuiRenderer] | type[FullscreenTuiRenderer]] = {
    TuiRendererKind.line: LineTuiRenderer,
    TuiRendererKind.fullscreen: FullscreenTuiRenderer,
}


def create_tui_renderer(kind: TuiRendererKind, console: Console | None = None) -> TuiRenderer:
    """Create a built-in TUI renderer."""

    return _BUILT_IN_RENDERERS[kind](console)


def _tui_help_text(*, approval_hint: str = "Tool approvals prompt with approve? [y/N].") -> str:
    """Shared TUI help text. ``approval_hint`` differs by renderer: the line and
    fullscreen renderers still read free-text `y`/`n` where blank/Enter denies, but
    the Textual renderer's decision panel defaults its highlight to "Approve once"
    (Enter approves) — see ``textual_renderer.help()`` for its override.
    """
    return (
        "Commands:\n"
        "  /help                    show this help\n"
        "  /auth [provider]         show credential status\n"
        "  /login [provider] [method]  login to a provider\n"
        "  /logout [provider]       remove stored provider credentials\n"
        "  /provider [provider]     show or switch provider for future prompts\n"
        "  /model [model]           show or switch model for future prompts\n"
        "  /quit, /exit             quit the TUI\n"
        "While a prompt is running, submitted input is queued as a follow-up.\n"
        f"{approval_hint}"
    )


def _wrap_transcript_line(line: str, *, width: int | None) -> list[str]:
    if width is None or width <= 0 or len(line) <= width:
        return [line]
    return [line[index : index + width] for index in range(0, len(line), width)]


def _compact_session_path(path: object) -> str:
    path_text = str(path)
    return os.path.basename(path_text) or path_text


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "(no output)")


def _markup_escape(value: object) -> str:
    return escape(str(value))


def _render_model_listing_text(
    entries: tuple[ModelCatalogProviderEntry, ...],
    *,
    current_provider: str,
    current_model: str | None,
    current_effort: str | None,
) -> str:
    """Render every catalog model grouped by provider, current one marked.

    Non-Textual fallback for `model_picker_request` -- no interactive picker
    outside the Textual renderer, so this is the same grouped-listing text
    `TuiShell._render_model_listing` prints for a bare `/model`, plus the
    active effort tier (which that shell-side listing predates and doesn't
    show). Deliberately does not track "pending configure" state the way the
    shell-side listing does -- a renderer has no access to that, only to
    what's passed here.
    """

    lines = ["Available models:"]
    for entry in entries:
        is_current_provider = entry.name == current_provider
        effective_model = current_model if current_model is not None else entry.default_model
        names = [
            f"{model_id} (current)"
            if is_current_provider and model_id == effective_model
            else model_id
            for model_id in entry.models
        ]
        lines.append(f"  {entry.name}: {', '.join(names)}")
    lines.append(f"Current model: {current_model or 'provider default'}")
    lines.append(f"Current provider: {current_provider}")
    lines.append(f"Current effort: {current_effort or 'provider default'}")
    lines.append("Use /model <id> [effort] to switch.")
    return "\n".join(lines)
