"""Command-line interface for Wisp."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import anyio
import typer
from rich.console import Console

from wisp.agent.loop import Agent
from wisp.config import WispConfig
from wisp.events import ErrorEvent, TokenDelta
from wisp.providers.base import ProviderError
from wisp.runtime.extensions import build_runtime
from wisp.runtime.registry import ToolRegistry, UnknownProviderError
from wisp.sessions.jsonl import JsonlSessionStore

READ_ONLY_PRINT_TOOL_NAMES = frozenset({"read", "grep", "find", "ls"})

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
) -> None:
    """Run Wisp in the initial print-mode CLI."""

    if ctx.invoked_subcommand is not None:
        return

    if prompt is None:
        # no_args_is_help normally handles this. This branch keeps direct calls
        # to the callback friendly in tests or embedded usage.
        raise typer.Exit(0)

    config = WispConfig.from_env(provider=provider, model=model, session_dir=session_dir)
    console = Console(stderr=True)
    try:
        anyio.run(_run_print, prompt, config)
    except (ProviderError, UnknownProviderError) as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1) from exc


def main() -> None:
    """Console-script entry point."""

    app()


async def _run_print(prompt: str, config: WispConfig) -> None:
    runtime = await build_runtime()
    provider = runtime.providers.get(config.provider)
    sessions = JsonlSessionStore(config.session_dir)
    agent = Agent(
        provider=provider,
        sessions=sessions,
        events=runtime.events,
        model=config.model,
        tool_registry=_print_mode_tool_registry(runtime.tools),
    )

    wrote_tokens = False
    async for event in agent.run(prompt):
        if isinstance(event, TokenDelta):
            sys.stdout.write(event.delta)
            sys.stdout.flush()
            wrote_tokens = True
        elif isinstance(event, ErrorEvent):
            raise ProviderError(event.message)

    if wrote_tokens:
        sys.stdout.write("\n")


def _print_mode_tool_registry(tools: ToolRegistry) -> ToolRegistry:
    """Return tools safe to expose without an interactive approval policy."""

    filtered = ToolRegistry()
    for tool in tools.all():
        if tool.name in READ_ONLY_PRINT_TOOL_NAMES:
            filtered.register(tool)
    return filtered
