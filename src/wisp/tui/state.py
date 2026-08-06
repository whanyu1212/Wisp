"""Shared TUI state and signal types."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import Literal

from wisp.agent.mode import AgentMode
from wisp.events import (
    CompactionCompleted,
    CompactionStarted,
    ContextBudget,
    ContextEstimated,
    KnownWispEvent,
    MessageCompleted,
    SessionCostSummary,
    SessionStatsReported,
    ToolApprovalRequested,
    TrustRequested,
    UsageCost,
)
from wisp.tui.rendering import TuiViewSnapshot


class TuiStatus(StrEnum):
    """High-level TUI interaction state."""

    idle = "idle"
    running = "running"
    compacting = "compacting"
    waiting_for_approval = "waiting_for_approval"
    confirming_all_tools = "confirming_all_tools"
    waiting_for_trust = "waiting_for_trust"
    exiting = "exiting"


@dataclass
class TuiInteractionState:
    """Mutable interaction state for the minimal TUI event loop."""

    status: TuiStatus = TuiStatus.idle
    current_command_id: str | None = None
    current_command_type: Literal["prompt", "compact"] | None = None
    shutdown_command_id: str | None = None
    pending_approval: ToolApprovalRequested | None = None
    pending_trust: TrustRequested | None = None
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
    mode: AgentMode = "build"
    input_hint: str = "wisp> "
    input_mode: str = "idle"
    queued_follow_ups: int = 0
    last_session: str | None = None
    cwd: str = field(default_factory=lambda: str(Path.cwd()))
    provider: str | None = None
    model: str | None = None
    context: ContextBudget | None = None
    cost: SessionCostSummary | None = None

    def snapshot(self) -> TuiViewSnapshot:
        """Return an immutable renderer-facing view snapshot."""

        return TuiViewSnapshot(
            status=self.status,
            mode=self.mode,
            input_hint=self.input_hint,
            input_mode=self.input_mode,
            queued_follow_ups=self.queued_follow_ups,
            last_session=self.last_session,
            cwd=self.cwd,
            provider=self.provider,
            model=self.model,
            context=self.context,
            cost=self.cost,
        )

    def update_context_from_event(self, event: KnownWispEvent) -> bool:
        """Apply context events and current provider observations to the footer state."""

        if isinstance(event, ContextEstimated):
            self.context = event.budget
            return True
        if isinstance(event, SessionStatsReported):
            self.context = event.stats.context
            self.cost = getattr(event.stats, "cost", None)
            return True
        if (
            isinstance(event, CompactionStarted)
            and event.reason in {"threshold", "overflow"}
            and event.trigger_budget is not None
        ):
            self.context = event.trigger_budget
            return True
        if isinstance(event, CompactionCompleted) and event.outcome == "completed":
            self.context = None
            self._update_cost(event.cost)
            return True
        if not isinstance(event, MessageCompleted):
            return False
        updated = False
        updated = self._update_cost(event.cost)
        if (
            event.finish_reason in {"error", "cancelled"}
            or event.usage is None
            or event.usage.total_tokens <= 0
            or self.context is None
        ):
            return updated

        observed_tokens = event.usage.total_tokens
        context_window = self.context.context_window
        remaining_tokens = (
            context_window - self.context.reserve_tokens - observed_tokens
            if context_window is not None
            else None
        )
        self.context = self.context.model_copy(
            update={
                "observed_tokens": observed_tokens,
                "observed_is_current": True,
                "remaining_tokens": remaining_tokens,
                "estimated_percent": (
                    observed_tokens / context_window * 100 if context_window is not None else None
                ),
                "over_budget": (
                    observed_tokens >= context_window - self.context.reserve_tokens
                    if context_window is not None
                    else None
                ),
            }
        )
        return True

    def _update_cost(self, cost: UsageCost | None) -> bool:
        if cost is None:
            return False
        current = self.cost or SessionCostSummary()
        if cost.estimated_usd is None:
            self.cost = current.model_copy(
                update={
                    "complete": False,
                    "unpriced_record_count": current.unpriced_record_count + 1,
                }
            )
        else:
            self.cost = current.model_copy(
                update={
                    "known_usd": current.known_usd + cost.estimated_usd,
                    "priced_record_count": current.priced_record_count + 1,
                }
            )
        return True


class _InputMode(StrEnum):
    idle = "idle"
    running = "running"
    approval = "approval"
    all_tools_confirmation = "all_tools_confirmation"
    trust = "trust"
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
    """Legacy line-renderer interrupt (normally ``KeyboardInterrupt``)."""

    mode: _InputMode


@dataclass(frozen=True)
class _InputCancelled:
    """Escape cancellation requested by a fullscreen frontend."""

    mode: _InputMode


@dataclass(frozen=True)
class _QuitPressed:
    """One timestamped Ctrl+C gesture from a double-press frontend."""

    mode: _InputMode
    pressed_at: float


class TuiCancelRequested(Exception):
    """Prompt-reader signal for an Escape cancellation request."""


class TuiQuitRequested(Exception):
    """Prompt-reader signal for one Ctrl+C quit gesture."""

    def __init__(self, *, pressed_at: float | None = None) -> None:
        super().__init__()
        self.pressed_at = monotonic() if pressed_at is None else pressed_at


@dataclass(frozen=True)
class _RpcEvent:
    event: KnownWispEvent


@dataclass(frozen=True)
class _RpcEventsClosed:
    error: str | None = None


type _TuiSignal = (
    _InputLine
    | _InputClosed
    | _InputInterrupted
    | _InputCancelled
    | _QuitPressed
    | _RpcEvent
    | _RpcEventsClosed
)


def _coerce_input_mode(value: str, *, fallback: _InputMode) -> _InputMode:
    try:
        return _InputMode(value)
    except ValueError:
        return fallback


def _input_mode_for_status(status: TuiStatus) -> _InputMode:
    if status is TuiStatus.waiting_for_approval:
        return _InputMode.approval
    if status is TuiStatus.confirming_all_tools:
        return _InputMode.all_tools_confirmation
    if status is TuiStatus.waiting_for_trust:
        return _InputMode.trust
    if status in {TuiStatus.running, TuiStatus.compacting}:
        return _InputMode.running
    if status is TuiStatus.exiting:
        return _InputMode.exiting
    return _InputMode.idle


def _view_status_for_status(status: TuiStatus) -> str:
    if status is TuiStatus.waiting_for_approval:
        return "waiting for approval"
    if status is TuiStatus.confirming_all_tools:
        return "confirming YOLO mode"
    if status is TuiStatus.waiting_for_trust:
        return "waiting for trust"
    return status.value


def _prompt_for_mode(mode: _InputMode) -> str:
    if mode is _InputMode.approval:
        return "approve? [y once/t tool/a all/N] "
    if mode is _InputMode.all_tools_confirmation:
        return "enable YOLO for this run? [y/N] "
    if mode is _InputMode.trust:
        return "trust this project? [y/N] "
    if mode is _InputMode.running:
        return "wisp(running)> "
    if mode is _InputMode.exiting:
        return "wisp(exiting)> "
    return "wisp> "


def _prompt_for_status(status: TuiStatus) -> str:
    if status is TuiStatus.compacting:
        return "wisp(compacting)> "
    return _prompt_for_mode(_input_mode_for_status(status))
