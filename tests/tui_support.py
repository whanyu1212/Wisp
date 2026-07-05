# ruff: noqa: F401

from __future__ import annotations

import asyncio
import builtins
import io
import sys
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
from rich.console import Console
from typer.testing import CliRunner

import wisp.tui.app as tui_app_module
from wisp import tui as tui_module
from wisp.cli import app
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
from wisp.tui import (
    FullscreenTuiRenderer,
    LineTuiRenderer,
    LiveFullscreenInputInterrupted,
    LiveFullscreenTui,
    TuiInteractionState,
    TuiOptions,
    TuiRendererKind,
    TuiShell,
    TuiStatus,
    TuiViewSnapshot,
    create_tui_renderer,
)
from wisp.tui.app import (
    _default_prompt_reader,
    _InputInterrupted,
    _InputLine,
    _InputMode,
    _rpc_command,
)

type EventBatch = list[KnownWispEvent]
type ScriptedBatch = EventBatch | tuple[float, EventBatch]


class ScriptedController:
    def __init__(
        self,
        prompt_events: list[ScriptedBatch] | None = None,
        *,
        approval_events: list[ScriptedBatch] | None = None,
        cancel_events: list[ScriptedBatch] | None = None,
        shutdown_events: list[ScriptedBatch] | None = None,
        close_after_prompt: bool = False,
    ) -> None:
        self.prompt_events = deque(prompt_events or [])
        self.approval_events = deque(approval_events or [])
        self.cancel_events = deque(cancel_events or [])
        self.shutdown_events = deque(shutdown_events or [])
        self.close_after_prompt = close_after_prompt
        self.prompts: list[str] = []
        self.approvals: list[tuple[str, bool, str | None]] = []
        self.cancelled: list[str] = []
        self.shutdown_count = 0
        self.closed = False
        self._send, self._receive = anyio.create_memory_object_stream[KnownWispEvent](100)

    async def prompt(self, prompt: str, *, command_id: str | None = None) -> str:
        self.prompts.append(prompt)
        selected_id = command_id or f"prompt-{len(self.prompts)}"
        await self._emit_scripted(
            self.prompt_events,
            default=[RpcCommandFinished(command_id=selected_id, command_type="prompt", ok=True)],
        )
        if self.close_after_prompt:
            await self._send.aclose()
        return selected_id

    async def cancel(self, target_id: str, *, command_id: str | None = None) -> str:
        self.cancelled.append(target_id)
        await self._emit_scripted(self.cancel_events, default=[])
        return command_id or f"cancel-{len(self.cancelled)}"

    async def approve(
        self,
        call_id: str,
        *,
        approved: bool = True,
        reason: str | None = None,
        command_id: str | None = None,
    ) -> str:
        self.approvals.append((call_id, approved, reason))
        await self._emit_scripted(self.approval_events, default=[])
        return command_id or f"approval-{len(self.approvals)}"

    async def shutdown(self, *, command_id: str | None = None) -> str:
        self.shutdown_count += 1
        selected_id = command_id or f"shutdown-{self.shutdown_count}"
        await self._emit_scripted(
            self.shutdown_events,
            default=[RpcCommandFinished(command_id=selected_id, command_type="shutdown", ok=True)],
        )
        return selected_id

    def events(self) -> AsyncIterator[KnownWispEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[KnownWispEvent]:
        async with self._receive.clone() as receive:
            async for event in receive:
                yield event

    async def close(self) -> None:
        self.closed = True
        await self._send.aclose()

    async def emit(self, events: EventBatch) -> None:
        await self._emit(events)

    async def _emit_scripted(
        self,
        batches: deque[ScriptedBatch],
        *,
        default: EventBatch,
    ) -> None:
        batch = batches.popleft() if batches else default
        if isinstance(batch, tuple):
            delay, events = batch
            asyncio.create_task(self._emit_after(delay, events))
            return
        await self._emit(batch)

    async def _emit_after(self, delay: float, events: EventBatch) -> None:
        await anyio.sleep(delay)
        await self._emit(events)

    async def _emit(self, events: EventBatch) -> None:
        for event in events:
            await self._send.send(event)


async def _reader_from(inputs: list[str]) -> object:
    values = deque(inputs)

    async def read(_prompt: str) -> str:
        if not values:
            raise EOFError
        return values.popleft()

    return read


def _console() -> tuple[Console, io.StringIO]:
    output = io.StringIO()
    return Console(file=output, force_terminal=False, width=120), output


__all__ = [
    "AssistantMessage",
    "AsyncIterator",
    "CliRunner",
    "Console",
    "ErrorEvent",
    "EventBatch",
    "FullscreenTuiRenderer",
    "KnownWispEvent",
    "LineTuiRenderer",
    "LiveFullscreenInputInterrupted",
    "LiveFullscreenTui",
    "Path",
    "RpcCommandFinished",
    "ScriptedBatch",
    "ScriptedController",
    "SessionSaved",
    "TokenDelta",
    "ToolApprovalRequested",
    "ToolApprovalResolved",
    "ToolCallRequested",
    "ToolResultReady",
    "TuiInteractionState",
    "TuiOptions",
    "TuiRendererKind",
    "TuiShell",
    "TuiStatus",
    "TuiViewSnapshot",
    "WispConfig",
    "_InputInterrupted",
    "_InputLine",
    "_InputMode",
    "_console",
    "_default_prompt_reader",
    "_reader_from",
    "_rpc_command",
    "anyio",
    "app",
    "asyncio",
    "builtins",
    "create_tui_renderer",
    "deque",
    "io",
    "sys",
    "tui_app_module",
    "tui_module",
]
