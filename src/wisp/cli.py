"""Command-line interface for Wisp."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import anyio
import typer
from rich.console import Console

from wisp.agent.loop import Agent
from wisp.config import WispConfig
from wisp.events import (
    ErrorEvent,
    SessionSaved,
    TokenDelta,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolResultReady,
    WispEvent,
)
from wisp.providers.base import ProviderError
from wisp.runtime.extensions import build_runtime
from wisp.runtime.registry import ToolRegistry, UnknownProviderError, UnknownToolError
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore, SessionError
from wisp.tools.approval import ToolApprovalPolicy

app = typer.Typer(
    add_completion=False,
    help="Wisp: a Python, Pi-inspired coding agent.",
    invoke_without_command=True,
    no_args_is_help=True,
)


@app.callback()
def cli_callback(
    ctx: typer.Context,
    prompt: Annotated[
        str | None,
        typer.Option("--prompt", "-p", help="Run one prompt and exit."),
    ] = None,
    provider: Annotated[
        str | None,
        typer.Option(help="Provider to use, e.g. fake or openai."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(help="Model name for the selected provider."),
    ] = None,
    session_dir: Annotated[
        Path | None,
        typer.Option(help="Directory for JSONL session files."),
    ] = None,
    allow_read_tools: Annotated[
        bool,
        typer.Option(help="Expose sandboxed read-only tools in print mode."),
    ] = False,
    allow_tool: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-tool",
            help="Expose a specific tool in print mode. Can be repeated.",
        ),
    ] = None,
    resume: Annotated[
        str | None,
        typer.Option(
            "--resume", help="Continue a session by JSONL path, filename, id, or id prefix."
        ),
    ] = None,
    continue_latest: Annotated[
        bool,
        typer.Option("--continue", help="Continue the latest session in the session directory."),
    ] = False,
    approve_unsafe_tools: Annotated[
        bool,
        typer.Option(
            "--yes",
            "--allow-unsafe-tool-execution",
            help="Approve non-interactive execution of mutating and command tools.",
        ),
    ] = False,
) -> None:
    """Run Wisp in the initial print-mode CLI."""

    if ctx.invoked_subcommand is not None:
        return

    if prompt is None:
        # no_args_is_help normally handles this. This branch keeps direct calls
        # to the callback friendly in tests or embedded usage.
        raise typer.Exit(0)

    console = Console(stderr=True)
    if resume is not None and continue_latest:
        console.print("[red]error:[/red] use either --resume or --continue, not both")
        raise typer.Exit(1)

    config = WispConfig.from_env(provider=provider, model=model, session_dir=session_dir)
    try:
        anyio.run(
            _run_print,
            prompt,
            config,
            allow_read_tools,
            tuple(allow_tool or ()),
            resume,
            continue_latest,
            approve_unsafe_tools,
        )
    except (ProviderError, SessionError, UnknownProviderError, UnknownToolError) as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1) from exc


def main() -> None:
    """Console-script entry point."""

    app()


async def _run_print(
    prompt: str,
    config: WispConfig,
    allow_read_tools: bool = False,
    allowed_tools: tuple[str, ...] = (),
    resume: str | None = None,
    continue_latest: bool = False,
    approve_unsafe_tools: bool = False,
) -> None:
    runtime = await build_runtime()
    provider = runtime.providers.get(config.provider)
    sessions = JsonlSessionStore(config.session_dir)
    session = _session_for_print_run(sessions, resume=resume, continue_latest=continue_latest)
    history = session.read_messages() if session is not None else ()
    agent = Agent(
        provider=provider,
        sessions=sessions,
        events=runtime.events,
        model=config.model,
        tool_registry=_print_mode_tool_registry(
            runtime.tools,
            allow_read_tools=allow_read_tools,
            allowed_tools=allowed_tools,
        ),
        tool_approval_policy=_print_mode_tool_approval_policy(approve_unsafe_tools),
    )

    event_console = Console(stderr=True, soft_wrap=True)
    needs_stdout_newline = False
    async for event in agent.run(prompt, session=session, history=history):
        if isinstance(event, TokenDelta):
            sys.stdout.write(event.delta)
            sys.stdout.flush()
            needs_stdout_newline = True
        elif isinstance(event, ErrorEvent):
            raise ProviderError(event.message)
        else:
            if needs_stdout_newline:
                sys.stdout.write("\n")
                sys.stdout.flush()
                needs_stdout_newline = False
            _render_print_event(event, event_console)

    if needs_stdout_newline:
        sys.stdout.write("\n")


def _render_print_event(event: WispEvent, console: Console) -> None:
    line = _print_event_line(event)
    if line is not None:
        console.print(line, markup=False)


def _print_event_line(event: WispEvent) -> str | None:
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
        status = "✗" if event.is_error else "✓"
        return f"{status} tool {event.name}: {_format_event_output(event.output)}"
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


def _truncate_inline(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[: max(0, max_chars - 1)].rstrip()}…"


def _print_mode_tool_approval_policy(approve_unsafe_tools: bool) -> ToolApprovalPolicy:
    if approve_unsafe_tools:
        return ToolApprovalPolicy.approve_all()
    return ToolApprovalPolicy.require_approval()


def _session_for_print_run(
    sessions: JsonlSessionStore,
    *,
    resume: str | None,
    continue_latest: bool,
) -> JsonlSession | None:
    if resume is not None:
        return sessions.load(resume)
    if continue_latest:
        return sessions.latest()
    return None


def _print_mode_tool_registry(
    tools: ToolRegistry,
    *,
    allow_read_tools: bool = False,
    allowed_tools: tuple[str, ...] = (),
) -> ToolRegistry:
    """Return tools explicitly allowed for non-interactive print mode."""

    allowed_names = set(allowed_tools)
    for name in allowed_names:
        tools.get(name)

    filtered = ToolRegistry()
    for tool in tools.all():
        if tool.name in allowed_names or (allow_read_tools and tool.safety == "read"):
            filtered.register(tool)
    return filtered
