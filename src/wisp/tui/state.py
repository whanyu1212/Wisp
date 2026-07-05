"""Shared TUI state and signal types."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum

from wisp.events import KnownWispEvent, ToolApprovalRequested
from wisp.tui.rendering import TuiViewSnapshot


class TuiStatus(StrEnum):
    """High-level TUI interaction state."""

    idle = "idle"
    running = "running"
    waiting_for_approval = "waiting_for_approval"
    exiting = "exiting"


@dataclass
class TuiInteractionState:
    """Mutable interaction state for the minimal TUI event loop."""

    status: TuiStatus = TuiStatus.idle
    current_command_id: str | None = None
    shutdown_command_id: str | None = None
    pending_approval: ToolApprovalRequested | None = None
    queued_prompts: deque[str] = field(default_factory=deque)
    exit_requested: bool = False
    input_closed: bool = False
    cancel_requested: bool = False
    token_stream_started: bool = False
    rendered_tokens: bool = False


@dataclass
class TuiViewState:
    """Shell-owned renderer-visible TUI state."""

    status: str = "idle"
    input_hint: str = "wisp> "
    input_mode: str = "idle"
    queued_follow_ups: int = 0
    last_session: str | None = None

    def snapshot(self) -> TuiViewSnapshot:
        """Return an immutable renderer-facing view snapshot."""

        return TuiViewSnapshot(
            status=self.status,
            input_hint=self.input_hint,
            input_mode=self.input_mode,
            queued_follow_ups=self.queued_follow_ups,
            last_session=self.last_session,
        )


class _InputMode(StrEnum):
    idle = "idle"
    running = "running"
    approval = "approval"
    exiting = "exiting"


@dataclass(frozen=True)
class _InputLine:
    text: str
    mode: _InputMode


@dataclass(frozen=True)
class _InputClosed:
    mode: _InputMode


@dataclass(frozen=True)
class _InputInterrupted:
    mode: _InputMode


@dataclass(frozen=True)
class _RpcEvent:
    event: KnownWispEvent


@dataclass(frozen=True)
class _RpcEventsClosed:
    error: str | None = None


type _TuiSignal = _InputLine | _InputClosed | _InputInterrupted | _RpcEvent | _RpcEventsClosed


def _coerce_input_mode(value: str, *, fallback: _InputMode) -> _InputMode:
    try:
        return _InputMode(value)
    except ValueError:
        return fallback


def _input_mode_for_status(status: TuiStatus) -> _InputMode:
    if status is TuiStatus.waiting_for_approval:
        return _InputMode.approval
    if status is TuiStatus.running:
        return _InputMode.running
    if status is TuiStatus.exiting:
        return _InputMode.exiting
    return _InputMode.idle


def _view_status_for_status(status: TuiStatus) -> str:
    if status is TuiStatus.waiting_for_approval:
        return "waiting for approval"
    return status.value


def _prompt_for_mode(mode: _InputMode) -> str:
    if mode is _InputMode.approval:
        return "approve? [y/N] "
    if mode is _InputMode.running:
        return "wisp(running)> "
    if mode is _InputMode.exiting:
        return "wisp(exiting)> "
    return "wisp> "
