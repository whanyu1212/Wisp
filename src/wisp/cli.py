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
from wisp.runtime.extensions import build_runtime
from wisp.sessions.jsonl import JsonlSessionStore

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

    config = WispConfig.from_env(session_dir=session_dir)
    anyio.run(_run_print, prompt, config)


def main() -> None:
    """Console-script entry point."""

    app()


async def _run_print(prompt: str, config: WispConfig) -> None:
    console = Console(stderr=True)
    runtime = await build_runtime()
    provider = runtime.providers.get(config.provider)
    sessions = JsonlSessionStore(config.session_dir)
    agent = Agent(provider=provider, sessions=sessions, events=runtime.events)

    wrote_tokens = False
    async for event in agent.run(prompt):
        if isinstance(event, TokenDelta):
            sys.stdout.write(event.delta)
            sys.stdout.flush()
            wrote_tokens = True
        elif isinstance(event, ErrorEvent):
            console.print(f"[red]error:[/red] {event.message}")

    if wrote_tokens:
        sys.stdout.write("\n")
