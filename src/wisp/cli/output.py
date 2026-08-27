"""CLI output rendering helpers."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator, Callable
from typing import NoReturn

import typer
from rich.console import Console

from wisp.coding.costs import format_usage_cost
from wisp.events import (
    AgentCompleted,
    CompactionCompleted,
    CompactionStarted,
    ErrorEvent,
    SessionSaved,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolResultReady,
    UsageCost,
    WispEvent,
)
from wisp.tool_presentation import tool_result_status

from .types import OutputMode, _JsonOutputModeError

_PRINT_TOOL_GLYPHS = {
    "done": "✓",
    "error": "✗",
    "denied": "!",
    "cancelled": "⊘",
}


def _exit_with_error(message: str, *, mode: OutputMode, console: Console) -> NoReturn:
    if _writes_json_events(mode):
        _write_json_event(ErrorEvent(message=message))
    else:
        console.print(f"[red]error:[/red] {message}")
    raise typer.Exit(1)


def _writes_json_events(mode: OutputMode) -> bool:
    return mode in {OutputMode.json, OutputMode.rpc}


async def _render_json_events(
    events: AsyncIterator[WispEvent],
    *,
    write_event: Callable[[WispEvent], None] | None = None,
) -> None:
    selected_write_event = write_event or _write_json_event
    rendered_error: str | None = None
    terminal_failure: AgentCompleted | None = None
    try:
        async for event in events:
            selected_write_event(event)
            if isinstance(event, ErrorEvent):
                rendered_error = event.message
            elif isinstance(event, AgentCompleted) and event.outcome != "completed":
                terminal_failure = event
    except Exception as exc:
        if rendered_error is None:
            rendered_error = str(exc)
            selected_write_event(ErrorEvent(message=rendered_error))
        raise _JsonOutputModeError(rendered_error) from exc
    if terminal_failure is not None:
        raise _JsonOutputModeError(rendered_error or f"Agent run {terminal_failure.outcome}")


def _write_json_event(event: WispEvent) -> None:
    sys.stdout.write(f"{event.model_dump_json()}\n")
    sys.stdout.flush()


def _render_print_event(event: WispEvent, console: Console) -> None:
    line = _print_event_line(event)
    if line is not None:
        console.print(line, markup=False)


def _print_event_line(event: WispEvent) -> str | None:
    if isinstance(event, CompactionStarted) and event.reason == "threshold":
        return "Context threshold reached; compacting automatically..."
    if isinstance(event, CompactionStarted) and event.reason == "overflow":
        return "Context overflow detected; compacting before one retry..."
    if isinstance(event, CompactionCompleted) and event.reason == "threshold":
        if event.outcome == "completed":
            line = f"Automatically compacted {event.replaced_entry_count} context entries."
            if event.cost is not None:
                line += f" {format_usage_cost(event.cost)}."
            if event.error:
                line += f" Warning: {event.error}"
            return line
        if event.outcome == "cancelled":
            return "Automatic compaction cancelled."
        return f"Automatic compaction failed: {event.error or 'unknown error'}"
    if isinstance(event, CompactionCompleted) and event.reason == "overflow":
        if event.outcome == "completed":
            if event.will_retry:
                line = (
                    f"Compacted {event.replaced_entry_count} context entries; retrying request..."
                )
                if event.cost is not None:
                    line += f" {format_usage_cost(event.cost)}."
                return line
            return f"Context overflow recovery failed: {event.error or 'retry was not scheduled'}"
        if event.outcome == "cancelled":
            return "Context overflow recovery cancelled."
        return f"Context overflow recovery failed: {event.error or 'unknown error'}"
    if isinstance(event, ToolCallRequested):
        return f"→ tool {event.name} {_format_event_arguments(event.arguments)}"
    if isinstance(event, ToolApprovalRequested):
        return f"? approval required for {event.name} ({event.safety})"
    if isinstance(event, ToolApprovalResolved):
        if event.approved:
            return f"✓ approved {event.name}"
        reason = f": {event.reason}" if event.reason else ""
        return f"! denied {event.name}{reason}"
    if isinstance(event, ToolResultReady):
        presentation_status = tool_result_status(
            event.is_error,
            event.exit_code,
            process_state=event.process_state,
        )
        glyph = _PRINT_TOOL_GLYPHS[presentation_status]
        return f"{glyph} tool {event.name}: {_format_event_output(event.output)}"
    if isinstance(event, SessionSaved):
        return f"session saved: {event.path}"
    return None


def _format_event_arguments(arguments: dict[str, object]) -> str:
    if not arguments:
        return "{}"
    try:
        text = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(arguments)
    return _truncate_inline(text, 240)


def _format_event_output(output: str) -> str:
    first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
    return _truncate_inline(first_line or "(no output)", 240)


def _format_usage_cost(event_cost: UsageCost | None) -> str:
    """Format one optional typed cost snapshot for print-mode stderr."""

    return format_usage_cost(event_cost)


def _truncate_inline(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[: max(0, max_chars - 1)].rstrip()}…"
