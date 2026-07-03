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
from rich.markup import escape

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
    resume: str | None = None
    continue_latest: bool = False
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


class _TuiCommandStreamClosed(RuntimeError):
    """Raised when RPC events end before the requested command finishes."""

    def __init__(self, command_id: str) -> None:
        super().__init__(f"RPC event stream ended before command completed: {command_id}")
        self.command_id = command_id


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

            try:
                command_id = await self.controller.prompt(prompt)
            except Exception as exc:
                self.console.print(f"[red]failed to send prompt:[/red] {_markup_escape(exc)}")
                return
            try:
                exit_requested = await self._drain_prompt(command_id)
            except KeyboardInterrupt:
                self.console.print("\n[yellow]Cancelling current prompt...[/yellow]")
                try:
                    await self.controller.cancel(command_id)
                except Exception as exc:
                    self.console.print(f"[red]failed to send cancel:[/red] {_markup_escape(exc)}")
                    return
                try:
                    await self._drain_prompt(command_id)
                except _TuiCommandStreamClosed as exc:
                    self._render_stream_closed(exc)
                return
            except _TuiCommandStreamClosed as exc:
                self._render_stream_closed(exc)
                return
            if exit_requested:
                await self._shutdown()
                return

    async def _shutdown(self) -> None:
        try:
            shutdown_id = await self.controller.shutdown()
        except Exception as exc:
            self.console.print(f"[red]shutdown failed:[/red] {_markup_escape(exc)}")
            return
        try:
            await self._drain_until_finished(shutdown_id, handle_approvals=False)
        except _TuiCommandStreamClosed as exc:
            self._render_stream_closed(exc)

    async def _drain_prompt(self, command_id: str) -> bool:
        return await self._drain_until_finished(command_id, handle_approvals=True)

    async def _drain_until_finished(self, command_id: str, *, handle_approvals: bool) -> bool:
        token_stream_started = False
        exit_requested = False
        async for event in self.controller.events():
            if handle_approvals and isinstance(event, ToolApprovalRequested):
                if token_stream_started:
                    self.console.print()
                    token_stream_started = False
                should_continue = await self._handle_approval(event)
                exit_requested = exit_requested or not should_continue
                continue
            if isinstance(event, TokenDelta):
                token_stream_started = True
                self.console.print(event.delta, end="", markup=False, highlight=False)
                continue
            if token_stream_started:
                self.console.print()
                token_stream_started = False
                if isinstance(event, AssistantMessage):
                    continue
            self._render_event(event)
            if _is_finished(event, command_id):
                return exit_requested
        if token_stream_started:
            self.console.print()
        raise _TuiCommandStreamClosed(command_id)

    async def _handle_approval(self, event: ToolApprovalRequested) -> bool:
        self.console.print(
            "[yellow]? approval required[/yellow] "
            f"{_markup_escape(event.name)} ({_markup_escape(event.safety)}) "
            f"{_markup_escape(event.arguments)}"
        )
        try:
            answer = (await self.prompt_reader("approve? [y/N] ")).strip().lower()
        except EOFError:
            self.console.print("[yellow]Approval input closed; denying tool request.[/yellow]")
            await self._send_approval(
                event.call_id,
                approved=False,
                reason="Denied from TUI: input closed",
            )
            return False
        except KeyboardInterrupt:
            self.console.print("\n[yellow]Approval interrupted; denying tool request.[/yellow]")
            await self._send_approval(
                event.call_id,
                approved=False,
                reason="Denied from TUI: interrupted",
            )
            return False
        approved = answer in {"y", "yes"}
        reason = None if approved else "Denied from TUI"
        return await self._send_approval(event.call_id, approved=approved, reason=reason)

    async def _send_approval(
        self,
        call_id: str,
        *,
        approved: bool,
        reason: str | None,
    ) -> bool:
        try:
            await self.controller.approve(call_id, approved=approved, reason=reason)
        except Exception as exc:
            self.console.print(f"[red]failed to send approval:[/red] {_markup_escape(exc)}")
            return False
        return True

    def _render_help(self) -> None:
        self.console.print("Commands:")
        self.console.print("  /help        show this help")
        self.console.print("  /quit, /exit quit the TUI")
        self.console.print("Tool approvals prompt with approve? [y/N].")

    def _render_event(self, event: KnownWispEvent) -> None:
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
            self.console.print(f"[dim]session saved: {_markup_escape(event.path)}[/dim]")
        elif isinstance(event, RpcCommandFinished) and not event.ok:
            self.console.print(
                f"[red]command failed:[/red] {_markup_escape(event.error or event.command_id)}"
            )

    def _render_stream_closed(self, exc: _TuiCommandStreamClosed) -> None:
        self.console.print(f"[red]{_markup_escape(str(exc))}[/red]")


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
    if options.resume is not None:
        command.extend(("--resume", options.resume))
    if options.continue_latest:
        command.append("--continue")
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


def _markup_escape(value: object) -> str:
    return escape(str(value))
