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
    CompactionCompleted,
    CompactionStarted,
    ContextBudget,
    ContextEstimate,
    ErrorEvent,
    KnownWispEvent,
    MessageCompleted,
    MessageDelta,
    ModelProviderAutoSwitched,
    ProjectConfigApplied,
    RpcCommandFinished,
    SessionSaved,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolResultReady,
    TrustRequested,
)
from wisp.rpc.commands import ApprovalScope
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
    format_tui_footer_lines,
    format_tui_footer_text,
)
from wisp.tui.app import (
    _default_prompt_reader,
    _InputClosed,
    _InputInterrupted,
    _InputLine,
    _InputMode,
    _rpc_command,
)


def threshold_budget() -> ContextBudget:
    return ContextBudget(
        estimate=ContextEstimate(
            system_tokens=10,
            message_tokens=70,
            tool_schema_tokens=1,
            total_tokens=81,
        ),
        context_window=100,
        reserve_tokens=20,
        remaining_tokens=-1,
        estimated_percent=81,
        over_budget=True,
    )


type EventBatch = list[KnownWispEvent]
type ScriptedBatch = EventBatch | tuple[float, EventBatch]


def completed_message(*, content: str) -> MessageCompleted:
    """Build a completed assistant message event for renderer tests."""

    return MessageCompleted(turn=1, content=content, finish_reason="stop")


def message_delta(*, delta: str) -> MessageDelta:
    """Build a streaming assistant text event for renderer tests."""

    return MessageDelta(turn=1, delta=delta)


class ScriptedController:
    def __init__(
        self,
        prompt_events: list[ScriptedBatch] | None = None,
        *,
        approval_events: list[ScriptedBatch] | None = None,
        cancel_events: list[ScriptedBatch] | None = None,
        compact_events: list[ScriptedBatch] | None = None,
        configure_events: list[ScriptedBatch] | None = None,
        session_stats_events: list[ScriptedBatch] | None = None,
        shutdown_events: list[ScriptedBatch] | None = None,
        close_after_prompt: bool = False,
    ) -> None:
        self.prompt_events = deque(prompt_events or [])
        self.approval_events = deque(approval_events or [])
        self.cancel_events = deque(cancel_events or [])
        self.compact_events = deque(compact_events or [])
        self.configure_events = deque(configure_events or [])
        self.session_stats_events = deque(session_stats_events or [])
        self.shutdown_events = deque(shutdown_events or [])
        self.close_after_prompt = close_after_prompt
        self.prompts: list[str] = []
        self.compactions: list[str | None] = []
        self.approvals: list[tuple[str, bool, str | None]] = []
        self.approval_scopes: list[ApprovalScope | None] = []
        self.trusts: list[tuple[str, bool, str | None, bool]] = []
        self.cancelled: list[str] = []
        self.configurations: list[tuple[str | None, str | None, str | None, bool]] = []
        self.session_stats_requests: list[str] = []
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

    async def compact(
        self,
        instructions: str | None = None,
        *,
        command_id: str | None = None,
    ) -> str:
        self.compactions.append(instructions)
        selected_id = command_id or f"compact-{len(self.compactions)}"
        await self._emit_scripted(
            self.compact_events,
            default=[RpcCommandFinished(command_id=selected_id, command_type="compact", ok=True)],
        )
        return selected_id

    async def get_session_stats(self, *, command_id: str | None = None) -> str:
        selected_id = command_id or f"session-stats-{len(self.session_stats_requests) + 1}"
        self.session_stats_requests.append(selected_id)
        await self._emit_scripted(self.session_stats_events, default=[])
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
        scope: ApprovalScope | None = None,
        command_id: str | None = None,
    ) -> str:
        self.approvals.append((call_id, approved, reason))
        self.approval_scopes.append(scope)
        await self._emit_scripted(self.approval_events, default=[])
        return command_id or f"approval-{len(self.approvals)}"

    async def trust(
        self,
        request_id: str,
        *,
        trusted: bool,
        reason: str | None = None,
        transient: bool = False,
        command_id: str | None = None,
    ) -> str:
        self.trusts.append((request_id, trusted, reason, transient))
        await self._emit_scripted(self.prompt_events, default=[])
        return command_id or f"trust-{len(self.trusts)}"

    async def shutdown(self, *, command_id: str | None = None) -> str:
        self.shutdown_count += 1
        selected_id = command_id or f"shutdown-{self.shutdown_count}"
        await self._emit_scripted(
            self.shutdown_events,
            default=[RpcCommandFinished(command_id=selected_id, command_type="shutdown", ok=True)],
        )
        return selected_id

    async def configure(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        clear_effort: bool = False,
        command_id: str | None = None,
    ) -> str:
        self.configurations.append((provider, model, effort, clear_effort))
        selected_id = command_id or f"configure-{len(self.configurations)}"
        await self._emit_scripted(
            self.configure_events,
            default=[
                RpcCommandFinished(
                    command_id=selected_id,
                    command_type="configure",
                    ok=True,
                )
            ],
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
    "AsyncIterator",
    "CliRunner",
    "Console",
    "CompactionCompleted",
    "CompactionStarted",
    "ErrorEvent",
    "EventBatch",
    "FullscreenTuiRenderer",
    "KnownWispEvent",
    "LineTuiRenderer",
    "LiveFullscreenInputInterrupted",
    "LiveFullscreenTui",
    "ModelProviderAutoSwitched",
    "Path",
    "ProjectConfigApplied",
    "RpcCommandFinished",
    "ScriptedBatch",
    "ScriptedController",
    "SessionSaved",
    "ToolApprovalRequested",
    "ToolApprovalResolved",
    "ToolCallRequested",
    "ToolResultReady",
    "TrustRequested",
    "TuiInteractionState",
    "TuiOptions",
    "TuiRendererKind",
    "TuiShell",
    "TuiStatus",
    "TuiViewSnapshot",
    "WispConfig",
    "_InputInterrupted",
    "_InputClosed",
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
    "format_tui_footer_lines",
    "format_tui_footer_text",
    "io",
    "completed_message",
    "message_delta",
    "threshold_budget",
    "sys",
    "tui_app_module",
    "tui_module",
]
