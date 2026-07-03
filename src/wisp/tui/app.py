"""Minimal Rich-based TUI shell for Wisp.

This is intentionally small: it provides an interactive terminal shell that uses
`RpcController` rather than reaching into CLI internals. A future full-screen TUI
can replace the rendering layer while keeping this controller-facing flow.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import anyio
from rich.console import Console

from wisp.config import WispConfig
from wisp.events import (
    AssistantMessage,
    ErrorEvent,
    KnownWispEvent,
    RpcCommandFinished,
    SessionSaved,
    TokenDelta,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolResultReady,
)
from wisp.rpc import JsonlSubprocessRpcTransport, RpcController


@dataclass(frozen=True)
class TuiOptions:
    """Options used to start the Wisp TUI shell."""

    config: WispConfig
    allow_read_tools: bool = False
    allowed_tools: tuple[str, ...] = ()
    approve_unsafe_tools: bool = False
    max_tool_iterations: int | None = None


class TuiController(Protocol):
    """Controller surface consumed by the TUI shell."""

    async def prompt(self, prompt: str, *, command_id: str | None = None) -> str: ...

    async def cancel(self, target_id: str, *, command_id: str | None = None) -> str: ...

    async def approve(
        self,
        call_id: str,
        *,
        approved: bool = True,
        reason: str | None = None,
        command_id: str | None = None,
    ) -> str: ...

    async def shutdown(self, *, command_id: str | None = None) -> str: ...

    def events(self) -> AsyncIterator[KnownWispEvent]: ...

    async def close(self) -> None: ...


PromptReader = Callable[[str], Awaitable[str]]


async def run_tui(
    options: TuiOptions,
    *,
    console: Console | None = None,
    prompt_reader: PromptReader | None = None,
    controller: TuiController | None = None,
) -> None:
    """Run the minimal Wisp TUI shell."""

    selected_console = console or Console()
    selected_controller = controller
    owns_controller = selected_controller is None
    if selected_controller is None:
        transport = await JsonlSubprocessRpcTransport.start(_rpc_command(options), env=_rpc_env())
        selected_controller = RpcController(transport)

    shell = TuiShell(
        selected_controller,
        console=selected_console,
        prompt_reader=prompt_reader,
    )
    try:
        await shell.run()
    finally:
        if owns_controller:
            await selected_controller.close()


class TuiShell:
    """Small prompt/event shell that drives Wisp through `RpcController`."""

    def __init__(
        self,
        controller: TuiController,
        *,
        console: Console | None = None,
        prompt_reader: PromptReader | None = None,
    ) -> None:
        self.controller = controller
        self.console = console or Console()
        self.prompt_reader = prompt_reader or _default_prompt_reader

    async def run(self) -> None:
        """Run the interactive prompt loop."""

        self.console.print("[bold cyan]Wisp TUI MVP[/bold cyan]")
        self.console.print("Type a prompt, /help, /quit, or press Ctrl-C to exit.")
        while True:
            try:
                prompt = (await self.prompt_reader("wisp> ")).strip()
            except (EOFError, KeyboardInterrupt):
                await self._shutdown()
                return
            if not prompt:
                continue
            if prompt in {"/quit", "/exit", ":q"}:
                await self._shutdown()
                return
            if prompt == "/help":
                self._render_help()
                continue

            command_id = await self.controller.prompt(prompt)
            try:
                await self._drain_prompt(command_id)
            except KeyboardInterrupt:
                self.console.print("\n[yellow]Cancelling current prompt...[/yellow]")
                await self.controller.cancel(command_id)
                await self._drain_prompt(command_id)

    async def _shutdown(self) -> None:
        shutdown_id = await self.controller.shutdown()
        async for event in self.controller.events():
            self._render_event(event)
            if _is_finished(event, shutdown_id):
                return

    async def _drain_prompt(self, command_id: str) -> None:
        token_stream_started = False
        async for event in self.controller.events():
            if isinstance(event, ToolApprovalRequested):
                if token_stream_started:
                    self.console.print()
                    token_stream_started = False
                await self._handle_approval(event)
                continue
            if isinstance(event, TokenDelta):
                token_stream_started = True
                self.console.print(event.delta, end="", markup=False, highlight=False)
                continue
            if isinstance(event, AssistantMessage) and token_stream_started:
                self.console.print()
                token_stream_started = False
                continue
            self._render_event(event)
            if _is_finished(event, command_id):
                return

    async def _handle_approval(self, event: ToolApprovalRequested) -> None:
        self.console.print(
            f"[yellow]? approval required[/yellow] {event.name} ({event.safety}) {event.arguments}"
        )
        answer = (await self.prompt_reader("approve? [y/N] ")).strip().lower()
        approved = answer in {"y", "yes"}
        reason = None if approved else "Denied from TUI"
        await self.controller.approve(event.call_id, approved=approved, reason=reason)

    def _render_help(self) -> None:
        self.console.print("Commands:")
        self.console.print("  /help        show this help")
        self.console.print("  /quit, /exit quit the TUI")
        self.console.print("Tool approvals prompt with approve? [y/N].")

    def _render_event(self, event: KnownWispEvent) -> None:
        if isinstance(event, AssistantMessage):
            self.console.print(event.content, markup=False, highlight=False)
        elif isinstance(event, ToolCallRequested):
            self.console.print(f"[blue]→ tool[/blue] {event.name} {event.arguments}")
        elif isinstance(event, ToolApprovalResolved):
            if event.approved:
                self.console.print(f"[green]✓ approved[/green] {event.name}")
            else:
                reason = f": {event.reason}" if event.reason else ""
                self.console.print(f"[red]! denied[/red] {event.name}{reason}")
        elif isinstance(event, ToolResultReady):
            status = "[red]✗[/red]" if event.is_error else "[green]✓[/green]"
            self.console.print(f"{status} tool {event.name}: {_first_line(event.output)}")
        elif isinstance(event, ErrorEvent):
            self.console.print(f"[red]error:[/red] {event.message}")
        elif isinstance(event, SessionSaved):
            self.console.print(f"[dim]session saved: {event.path}[/dim]")
        elif isinstance(event, RpcCommandFinished) and not event.ok:
            self.console.print(f"[red]command failed:[/red] {event.error or event.command_id}")


async def _default_prompt_reader(prompt: str) -> str:
    return await anyio.to_thread.run_sync(input, prompt)


def _rpc_command(options: TuiOptions) -> tuple[str, ...]:
    command: list[str] = [
        sys.executable,
        "-m",
        "wisp",
        "--mode",
        "rpc",
        "--provider",
        options.config.provider,
        "--session-dir",
        str(options.config.session_dir),
    ]
    if options.config.model is not None:
        command.extend(("--model", options.config.model))
    if options.allow_read_tools:
        command.append("--allow-read-tools")
    for tool_name in options.allowed_tools:
        command.extend(("--allow-tool", tool_name))
    if options.approve_unsafe_tools:
        command.append("--yes")
    if options.max_tool_iterations is not None:
        command.extend(("--max-tool-iterations", str(options.max_tool_iterations)))
    return tuple(command)


def _rpc_env() -> dict[str, str]:
    return dict(os.environ)


def _is_finished(event: KnownWispEvent, command_id: str) -> bool:
    return isinstance(event, RpcCommandFinished) and event.command_id == command_id


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "(no output)")
