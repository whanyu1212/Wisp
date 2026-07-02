"""Command-line interface for Wisp."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from enum import StrEnum
from pathlib import Path
from typing import Annotated, cast

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


class OutputMode(StrEnum):
    """Non-interactive output modes."""

    text = "text"
    json = "json"
    rpc = "rpc"


class _JsonOutputModeError(ProviderError):
    """Raised after JSONL output has already emitted a model-visible error event."""


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
    mode: Annotated[
        OutputMode,
        typer.Option("--mode", help="Output mode: text, JSONL events, or RPC."),
    ] = OutputMode.text,
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
    max_tool_iterations: Annotated[
        int | None,
        typer.Option(
            "--max-tool-iterations",
            help="Optional cap on model/tool rounds. Defaults to uncapped.",
        ),
    ] = None,
) -> None:
    """Run Wisp in the initial print-mode CLI."""

    if ctx.invoked_subcommand is not None:
        return

    console = Console(stderr=True)
    if mode is OutputMode.rpc:
        if prompt is not None:
            _exit_with_error(
                "--prompt is not used with --mode rpc; send prompt commands on stdin",
                mode=mode,
                console=console,
            )
    elif prompt is None:
        # no_args_is_help normally handles this. This branch keeps direct calls
        # to the callback friendly in tests or embedded usage.
        raise typer.Exit(0)

    if resume is not None and continue_latest:
        _exit_with_error(
            "use either --resume or --continue, not both",
            mode=mode,
            console=console,
        )
    if max_tool_iterations is not None and max_tool_iterations < 0:
        _exit_with_error(
            "--max-tool-iterations must be non-negative",
            mode=mode,
            console=console,
        )

    config = WispConfig.from_env(provider=provider, model=model, session_dir=session_dir)
    try:
        if mode is OutputMode.rpc:
            anyio.run(
                _run_rpc,
                config,
                allow_read_tools,
                tuple(allow_tool or ()),
                resume,
                continue_latest,
                approve_unsafe_tools,
                max_tool_iterations,
            )
        else:
            assert prompt is not None
            anyio.run(
                _run_print,
                prompt,
                config,
                allow_read_tools,
                tuple(allow_tool or ()),
                resume,
                continue_latest,
                approve_unsafe_tools,
                max_tool_iterations,
                mode,
            )
    except _JsonOutputModeError as exc:
        raise typer.Exit(1) from exc
    except (ProviderError, SessionError, UnknownProviderError, UnknownToolError) as exc:
        if _writes_json_events(mode):
            _write_json_event(ErrorEvent(message=str(exc)))
        else:
            console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1) from exc


def main() -> None:
    """Console-script entry point."""

    app()


def _exit_with_error(message: str, *, mode: OutputMode, console: Console) -> None:
    if _writes_json_events(mode):
        _write_json_event(ErrorEvent(message=message))
    else:
        console.print(f"[red]error:[/red] {message}")
    raise typer.Exit(1)


def _writes_json_events(mode: OutputMode) -> bool:
    return mode in {OutputMode.json, OutputMode.rpc}


async def _run_print(
    prompt: str,
    config: WispConfig,
    allow_read_tools: bool = False,
    allowed_tools: tuple[str, ...] = (),
    resume: str | None = None,
    continue_latest: bool = False,
    approve_unsafe_tools: bool = False,
    max_tool_iterations: int | None = None,
    mode: OutputMode = OutputMode.text,
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
        max_tool_iterations=max_tool_iterations,
    )

    events = agent.run(prompt, session=session, history=history)
    if mode is OutputMode.json:
        await _render_json_events(events)
        return

    event_console = Console(stderr=True, soft_wrap=True)
    wrote_tokens = False
    stderr_needs_separator = False
    async for event in events:
        if isinstance(event, TokenDelta):
            sys.stdout.write(event.delta)
            sys.stdout.flush()
            wrote_tokens = True
            stderr_needs_separator = True
        elif isinstance(event, ErrorEvent):
            raise ProviderError(event.message)
        else:
            if stderr_needs_separator and _print_event_line(event) is not None:
                event_console.print()
                stderr_needs_separator = False
            _render_print_event(event, event_console)

    if wrote_tokens:
        sys.stdout.write("\n")


async def _run_rpc(
    config: WispConfig,
    allow_read_tools: bool = False,
    allowed_tools: tuple[str, ...] = (),
    resume: str | None = None,
    continue_latest: bool = False,
    approve_unsafe_tools: bool = False,
    max_tool_iterations: int | None = None,
) -> None:
    runtime = await build_runtime()
    provider = runtime.providers.get(config.provider)
    sessions = JsonlSessionStore(config.session_dir)
    session = _session_for_print_run(sessions, resume=resume, continue_latest=continue_latest)
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
        max_tool_iterations=max_tool_iterations,
    )

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        command = _parse_rpc_command(line)
        if command is None:
            continue
        command_type = command.get("type")
        if command_type == "shutdown":
            return
        if command_type == "prompt":
            prompt = command.get("prompt")
            if not isinstance(prompt, str):
                _write_json_event(
                    ErrorEvent(message="RPC prompt command requires string field: prompt")
                )
                continue
            if session is None:
                session = sessions.create()
            history = session.read_messages() if session.path.is_file() else ()
            try:
                await _render_json_events(agent.run(prompt, session=session, history=history))
            except _JsonOutputModeError:
                continue
            continue
        _write_json_event(ErrorEvent(message=f"Unknown RPC command: {command_type}"))


def _parse_rpc_command(line: str) -> dict[str, object] | None:
    try:
        command = json.loads(line)
    except json.JSONDecodeError as exc:
        _write_json_event(ErrorEvent(message=f"Invalid RPC JSON: {exc.msg}"))
        return None
    if not isinstance(command, dict):
        _write_json_event(ErrorEvent(message="RPC command must be a JSON object"))
        return None
    return cast(dict[str, object], command)


async def _render_json_events(events: AsyncIterator[WispEvent]) -> None:
    rendered_error: str | None = None
    try:
        async for event in events:
            _write_json_event(event)
            if isinstance(event, ErrorEvent):
                rendered_error = event.message
    except Exception as exc:
        if rendered_error is None:
            rendered_error = str(exc)
            _write_json_event(ErrorEvent(message=rendered_error))
        raise _JsonOutputModeError(rendered_error) from exc


def _write_json_event(event: WispEvent) -> None:
    sys.stdout.write(f"{event.model_dump_json()}\n")
    sys.stdout.flush()


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
