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
from wisp.runtime.registry import ToolRegistry, UnknownProviderError, UnknownToolError
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
        anyio.run(
            _run_print,
            prompt,
            config,
            allow_read_tools,
            tuple(allow_tool or ()),
        )
    except (ProviderError, UnknownProviderError, UnknownToolError) as exc:
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
) -> None:
    runtime = await build_runtime()
    provider = runtime.providers.get(config.provider)
    sessions = JsonlSessionStore(config.session_dir)
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
