"""Stateful provider-neutral harness built on the pure agent loop."""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

import anyio

from wisp.agent.configuration import validate_agent_runtime_limits
from wisp.agent.execution import (
    ContextOverflowHook,
    ContextOverflowSnapshot,
    RequestBoundaryDecision,
    RequestBoundarySnapshot,
    ToolExecutor,
)
from wisp.agent.loop import AgentLoopConfig, AgentLoopEvent, UsageCostEstimator, run_agent_loop
from wisp.agent.messages import (
    Message,
    completion_event_has_history,
    message_from_completion_event,
    normalize_provider_history,
)
from wisp.agent.transcript import plan_interrupted_tool_repairs
from wisp.events import (
    ErrorEvent,
    MessageCompleted,
    QueueKind,
    QueueMessageInjected,
    QueueMode,
    QueueUpdated,
    ToolExecutionEnded,
    TurnCompleted,
    TurnStarted,
)
from wisp.providers.base import Provider, ToolSpec

_MAX_PENDING_QUEUE_MESSAGES = 100


@dataclass(frozen=True, slots=True)
class AgentHarnessConfig:
    """Portable dependencies and limits for an `AgentHarness`."""

    provider: Provider
    tool_executor: ToolExecutor
    model: str | None = None
    tools: tuple[ToolSpec, ...] = ()
    max_tool_iterations: int | None = None
    effort: str | None = None
    context_window: int | None = None
    context_reserve_tokens: int = 16_384
    context_pressure_threshold: float = 0.8
    cost_estimator: UsageCostEstimator | None = None
    steering_mode: QueueMode = "one_at_a_time"
    follow_up_mode: QueueMode = "one_at_a_time"
    max_pending_queue_messages: int = _MAX_PENDING_QUEUE_MESSAGES
    prompt_cache_key: str | None = None

    def __post_init__(self) -> None:
        """Reject invalid runtime settings even when callers bypass static typing."""
        validate_agent_runtime_limits(
            max_tool_iterations=self.max_tool_iterations,
            context_window=self.context_window,
            context_reserve_tokens=self.context_reserve_tokens,
            context_pressure_threshold=self.context_pressure_threshold,
        )
        _require_queue_mode(self.steering_mode)
        _require_queue_mode(self.follow_up_mode)
        if type(self.max_pending_queue_messages) is not int or self.max_pending_queue_messages < 0:
            raise ValueError("max_pending_queue_messages must be a non-negative integer")


type AgentHarnessEvent = AgentLoopEvent | QueueMessageInjected | QueueUpdated


def _require_queue_mode(mode: object) -> None:
    if not isinstance(mode, str) or mode not in {"one_at_a_time", "all"}:
        raise ValueError(f"Unsupported queue mode: {mode!r}")


def _active_turn_start(messages: Sequence[Message]) -> int | None:
    """Return the index of the still-running turn's user message, if any.

    The most recent ``user``-role message starts the turn currently in progress —
    every assistant/tool row after it is this turn's own in-flight work, not
    replayed prior-turn history. A transcript rebuild mid-turn (e.g. after
    auto-compaction replaces ``self._messages``) must keep those rows structured so
    the model retains a record of tool calls it just made in this same turn.
    """

    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "user":
            return index
    return None


@dataclass(frozen=True, slots=True)
class QueuedMessages:
    """Immutable snapshot of harness-owned queued user messages."""

    steering: tuple[Message, ...] = ()
    follow_up: tuple[Message, ...] = ()

    @property
    def count(self) -> int:
        """Return the total number of queued messages."""
        return len(self.steering) + len(self.follow_up)


class SimpleCancellationToken:
    """Small cooperative cancellation token owned by one harness run."""

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        """Request cancellation."""
        self._cancelled = True

    def is_cancelled(self) -> bool:
        """Return whether cancellation has been requested."""
        return self._cancelled


def _cancelled_events(
    active_turn: int | None,
    *,
    active_turn_completed: bool,
) -> tuple[AgentLoopEvent, ...]:
    events: list[AgentLoopEvent] = [ErrorEvent(message="Agent run cancelled")]
    if active_turn is not None and not active_turn_completed:
        events.append(
            TurnCompleted(
                turn=active_turn,
                outcome="cancelled",
                finish_reason="cancelled",
            )
        )
    return tuple(events)


@dataclass(slots=True)
class _HarnessRunState:
    """Public turn lifecycle state retained for one primary loop invocation."""

    active_turn: int | None = None
    active_turn_completed: bool = False
    had_tool_calls: bool = False

    def observe(self, event: AgentLoopEvent) -> None:
        if isinstance(event, TurnStarted):
            self.active_turn = event.turn
            self.active_turn_completed = False
            self.had_tool_calls = False
        elif isinstance(event, MessageCompleted) and event.tool_calls:
            self.had_tool_calls = True
        elif isinstance(event, TurnCompleted):
            self.active_turn_completed = True

    def cancelled_events(self) -> tuple[AgentLoopEvent, ...]:
        return _cancelled_events(
            self.active_turn,
            active_turn_completed=self.active_turn_completed,
        )


@dataclass(frozen=True, slots=True)
class HarnessBoundaryContext:
    """Harness state available to session-owned request-boundary preparation."""

    snapshot: RequestBoundarySnapshot
    messages: tuple[Message, ...]
    injected_messages: tuple[Message, ...]
    stop_by_default: bool


class HarnessBoundaryPreparer(Protocol):
    """Prepare compaction/rebase decisions without owning queues or the loop."""

    async def prepare_boundary(
        self, *, context: HarnessBoundaryContext
    ) -> RequestBoundaryDecision | None:
        """Return a complete loop decision, or ``None`` for harness defaults."""
        ...


@dataclass(frozen=True, slots=True)
class _ArmedRequestBoundary:
    """Queue effects exposed before the loop invokes its boundary hook."""

    turn: int
    had_tool_calls: bool
    injected_messages: tuple[Message, ...]
    stop_by_default: bool


class AgentHarness:
    """Own an in-memory transcript and delegate execution to the pure loop."""

    def __init__(
        self,
        config: AgentHarnessConfig,
        *,
        messages: Sequence[Message] = (),
    ) -> None:
        self._config = config
        self._messages = list(messages)
        self._current_token: SimpleCancellationToken | None = None
        self._current_scope: anyio.CancelScope | None = None
        self._running = False
        self._steering_queue: deque[Message] = deque()
        self._follow_up_queue: deque[Message] = deque()

    @property
    def config(self) -> AgentHarnessConfig:
        """Return the current harness configuration."""
        return self._config

    @property
    def messages(self) -> tuple[Message, ...]:
        """Return an immutable transcript snapshot."""
        return tuple(self._messages)

    @property
    def is_running(self) -> bool:
        """Return whether a prompt or continuation is active."""
        return self._running

    @property
    def queued_messages(self) -> QueuedMessages:
        """Return an immutable snapshot of both pending queues."""
        return QueuedMessages(
            steering=tuple(self._steering_queue),
            follow_up=tuple(self._follow_up_queue),
        )

    @property
    def pending_message_count(self) -> int:
        """Return the total number of pending steering and follow-up messages."""
        return self.queued_messages.count

    def has_queued_messages(self) -> bool:
        """Return whether either queue contains a pending message."""
        return bool(self._steering_queue or self._follow_up_queue)

    def replace_config(self, config: AgentHarnessConfig) -> None:
        """Replace provider/tool configuration between runs."""
        self._ensure_idle()
        self._config = config

    def append_message(self, message: Message) -> None:
        """Append restored or application-provided transcript state."""
        self._ensure_idle()
        self._messages.append(message)

    def replace_messages(self, messages: Sequence[Message]) -> None:
        """Replace the transcript between runs."""
        self._ensure_idle()
        self._messages = list(messages)

    def repair_interrupted_tool_calls(self) -> tuple[Message, ...]:
        """Repair logical ordering and return synthetic results needing persistence."""

        self._ensure_idle()
        plan = plan_interrupted_tool_repairs(self._messages)
        self._messages = list(plan.messages)
        return plan.repairs

    def cancel(self) -> bool:
        """Request cooperative cancellation for the active run."""
        if self._current_token is None:
            return False
        self._current_token.cancel()
        if self._current_scope is not None:
            self._current_scope.cancel()
        return True

    def steer(self, content: str) -> QueueUpdated:
        """Queue user text for injection after the current assistant/tool batch."""
        return self.steer_message(Message(role="user", content=content))

    def steer_message(self, message: Message) -> QueueUpdated:
        """Queue a user message for steering without changing the transcript."""
        self._require_user_queue_message(message)
        self._require_queue_capacity()
        self._steering_queue.append(message)
        return self.queue_updated_event()

    def follow_up(self, content: str) -> QueueUpdated:
        """Queue user text for injection when a run would otherwise stop."""
        return self.follow_up_message(Message(role="user", content=content))

    def follow_up_message(self, message: Message) -> QueueUpdated:
        """Queue a user message for follow-up without changing the transcript."""
        self._require_user_queue_message(message)
        self._require_queue_capacity()
        self._follow_up_queue.append(message)
        return self.queue_updated_event()

    def set_steering_mode(self, mode: QueueMode) -> QueueUpdated:
        """Set how many steering messages a future drain will inject."""
        self._config = replace(self._config, steering_mode=mode)
        return self.queue_updated_event()

    def set_follow_up_mode(self, mode: QueueMode) -> QueueUpdated:
        """Set how many follow-up messages a future drain will inject."""
        self._config = replace(self._config, follow_up_mode=mode)
        return self.queue_updated_event()

    def pop_latest_steering(self) -> Message | None:
        """Remove and return the latest steering message for editing."""
        if not self._steering_queue:
            return None
        return self._steering_queue.pop()

    def pop_latest_follow_up(self) -> Message | None:
        """Remove and return the latest follow-up message for editing."""
        if not self._follow_up_queue:
            return None
        return self._follow_up_queue.pop()

    def clear_queue(self, kind: QueueKind) -> tuple[Message, ...]:
        """Clear one queue and return its previous contents in FIFO order."""
        queue = self._queue_for(kind)
        cleared = tuple(queue)
        queue.clear()
        return cleared

    def clear_queues(self) -> QueuedMessages:
        """Clear both queues and return their previous contents."""
        cleared = self.queued_messages
        self._steering_queue.clear()
        self._follow_up_queue.clear()
        return cleared

    def drain_steering(self) -> tuple[QueueMessageInjected | QueueUpdated, ...]:
        """Inject the next steering batch before a provider request starts."""

        self._ensure_idle()
        drain_batch = self._queued_batch("steering")
        if not drain_batch:
            return ()

        events: list[QueueMessageInjected | QueueUpdated] = []
        for message in drain_batch:
            event = self._inject_queued_message("steering", message)
            if event is not None:
                events.append(event)
        events.append(self.queue_updated_event())
        return tuple(events)

    def queue_updated_event(self) -> QueueUpdated:
        """Return current queue state as a portable versioned event."""
        return QueueUpdated(
            steering=tuple(message.user_visible_content for message in self._steering_queue),
            follow_up=tuple(message.user_visible_content for message in self._follow_up_queue),
            steering_mode=self._config.steering_mode,
            follow_up_mode=self._config.follow_up_mode,
        )

    def prompt(
        self,
        content: str,
        *,
        turn_offset: int = 0,
        tool_iteration_offset: int = 0,
        defer_context_overflow_errors: bool = False,
        boundary_preparer: HarnessBoundaryPreparer | None = None,
        context_overflow_hook: ContextOverflowHook | None = None,
    ) -> AsyncGenerator[AgentHarnessEvent, None]:
        """Append a user message and start a run."""
        return self.prompt_message(
            Message(role="user", content=content),
            turn_offset=turn_offset,
            tool_iteration_offset=tool_iteration_offset,
            defer_context_overflow_errors=defer_context_overflow_errors,
            boundary_preparer=boundary_preparer,
            context_overflow_hook=context_overflow_hook,
        )

    def prompt_message(
        self,
        message: Message,
        *,
        turn_offset: int = 0,
        tool_iteration_offset: int = 0,
        defer_context_overflow_errors: bool = False,
        boundary_preparer: HarnessBoundaryPreparer | None = None,
        context_overflow_hook: ContextOverflowHook | None = None,
    ) -> AsyncGenerator[AgentHarnessEvent, None]:
        """Append an existing user message and start a run."""
        if message.role != "user":
            raise ValueError("AgentHarness prompts require a user message")
        return self._run(
            prompt_message=message,
            turn_offset=turn_offset,
            tool_iteration_offset=tool_iteration_offset,
            defer_context_overflow_errors=defer_context_overflow_errors,
            boundary_preparer=boundary_preparer,
            context_overflow_hook=context_overflow_hook,
        )

    def continue_(
        self,
        *,
        turn_offset: int = 0,
        tool_iteration_offset: int = 0,
        defer_context_overflow_errors: bool = False,
        boundary_preparer: HarnessBoundaryPreparer | None = None,
        context_overflow_hook: ContextOverflowHook | None = None,
    ) -> AsyncGenerator[AgentHarnessEvent, None]:
        """Continue from the current transcript without adding a user message."""
        return self._run(
            turn_offset=turn_offset,
            tool_iteration_offset=tool_iteration_offset,
            defer_context_overflow_errors=defer_context_overflow_errors,
            boundary_preparer=boundary_preparer,
            context_overflow_hook=context_overflow_hook,
        )

    async def _run(
        self,
        *,
        prompt_message: Message | None = None,
        turn_offset: int = 0,
        tool_iteration_offset: int = 0,
        defer_context_overflow_errors: bool = False,
        boundary_preparer: HarnessBoundaryPreparer | None = None,
        context_overflow_hook: ContextOverflowHook | None = None,
    ) -> AsyncGenerator[AgentHarnessEvent, None]:
        self.repair_interrupted_tool_calls()
        self._running = True
        token = SimpleCancellationToken()
        self._current_token = token
        if prompt_message is not None:
            self._messages.append(prompt_message)

        run = _HarnessRunState()
        armed_boundary: _ArmedRequestBoundary | None = None
        pending_transcript_transition: (
            tuple[RequestBoundaryDecision, tuple[Message, ...]] | None
        ) = None

        class _BoundaryHook:
            async def before_next_request(
                _self, *, snapshot: RequestBoundarySnapshot
            ) -> RequestBoundaryDecision:
                nonlocal armed_boundary, pending_transcript_transition
                boundary = armed_boundary
                if boundary is None:
                    raise RuntimeError("AgentHarness received an unarmed request boundary")
                if (
                    snapshot.turn != boundary.turn
                    or snapshot.had_tool_calls != boundary.had_tool_calls
                ):
                    raise RuntimeError(
                        "AgentHarness request boundary did not match its completed turn"
                    )
                armed_boundary = None
                if boundary_preparer is not None:
                    decision = await boundary_preparer.prepare_boundary(
                        context=HarnessBoundaryContext(
                            snapshot=snapshot,
                            messages=tuple(
                                message.model_copy(deep=True) for message in self._messages
                            ),
                            injected_messages=boundary.injected_messages,
                            stop_by_default=boundary.stop_by_default,
                        )
                    )
                    if decision is not None:
                        if decision.messages is not None or decision.context_rebase is not None:
                            pending_transcript_transition = (
                                decision,
                                snapshot.continuation_messages,
                            )
                        return decision
                if boundary.injected_messages:
                    if snapshot.can_append_user_messages:
                        return RequestBoundaryDecision(extra_messages=boundary.injected_messages)
                    # Cursor-less structured history cannot be flattened into
                    # extras. Replace from the complete normalized transcript,
                    # retaining assistant/tool pairs atomically.
                    return RequestBoundaryDecision(
                        messages=normalize_provider_history(
                            self._messages, active_from=_active_turn_start(self._messages)
                        )
                    )
                return RequestBoundaryDecision(stop=boundary.stop_by_default)

        class _OverflowHook:
            async def recover_context_overflow(
                _self, *, snapshot: ContextOverflowSnapshot
            ) -> RequestBoundaryDecision | None:
                nonlocal pending_transcript_transition
                assert context_overflow_hook is not None
                decision = await context_overflow_hook.recover_context_overflow(snapshot=snapshot)
                if decision is not None and (
                    decision.messages is not None or decision.context_rebase is not None
                ):
                    pending_transcript_transition = (decision, snapshot.continuation_messages)
                return decision

        config = AgentLoopConfig(
            provider=self._config.provider,
            tool_executor=self._config.tool_executor,
            model=self._config.model,
            tools=self._config.tools,
            max_tool_iterations=self._config.max_tool_iterations,
            cancellation_token=token,
            effort=self._config.effort,
            prompt_cache_key=self._config.prompt_cache_key,
            context_window=self._config.context_window,
            context_reserve_tokens=self._config.context_reserve_tokens,
            context_pressure_threshold=self._config.context_pressure_threshold,
            turn_offset=turn_offset,
            tool_iteration_offset=tool_iteration_offset,
            cost_estimator=self._config.cost_estimator,
            defer_context_overflow_errors=defer_context_overflow_errors,
            request_boundary_hook=_BoundaryHook(),
            context_overflow_hook=_OverflowHook() if context_overflow_hook is not None else None,
        )
        provider_messages = normalize_provider_history(
            self._messages, active_from=_active_turn_start(self._messages)
        )
        loop_events = run_agent_loop(config, messages=provider_messages)
        try:
            while True:
                if token.is_cancelled():
                    for cancellation_event in run.cancelled_events():
                        yield cancellation_event
                    return

                scope = anyio.CancelScope()
                self._current_scope = scope
                event: AgentLoopEvent | None = None
                stream_ended = False
                with scope:
                    try:
                        event = await anext(loop_events)
                    except StopAsyncIteration:
                        stream_ended = True
                if self._current_scope is scope:
                    self._current_scope = None

                if scope.cancel_called:
                    for cancellation_event in run.cancelled_events():
                        yield cancellation_event
                    return
                if stream_ended:
                    break
                assert event is not None

                run.observe(event)
                if isinstance(event, TurnStarted) and pending_transcript_transition is not None:
                    decision, continuation_messages = pending_transcript_transition
                    self._apply_transcript_transition(
                        decision, continuation_messages=continuation_messages
                    )
                    pending_transcript_transition = None
                if isinstance(
                    event, MessageCompleted | ToolExecutionEnded
                ) and completion_event_has_history(event):
                    # ToolResultReady copies the terminal tool payload; retain it now
                    # so closing at this visible boundary cannot lose output. Empty
                    # failed assistant completions settle lifecycle state only.
                    self._messages.append(message_from_completion_event(event))
                yield event

                if not isinstance(event, TurnCompleted) or event.outcome != "completed":
                    continue

                queue_kind: QueueKind | None = None
                if self._steering_queue:
                    queue_kind = "steering"
                elif not run.had_tool_calls and self._follow_up_queue:
                    queue_kind = "follow_up"

                injected_messages: list[Message] = []
                if queue_kind is not None:
                    drain_batch = self._queued_batch(queue_kind)
                    for message in drain_batch:
                        if token.is_cancelled():
                            for cancellation_event in run.cancelled_events():
                                yield cancellation_event
                            return
                        injected_event = self._inject_queued_message(queue_kind, message)
                        if injected_event is not None:
                            injected_messages.append(message)
                            yield injected_event
                    # Queue entries added after the boundary snapshot wait for
                    # a later boundary, while edits to snapshotted entries are
                    # visible between each individual injected event.
                    yield self.queue_updated_event()
                    if token.is_cancelled():
                        for cancellation_event in run.cancelled_events():
                            yield cancellation_event
                        return

                armed_boundary = _ArmedRequestBoundary(
                    turn=event.turn,
                    had_tool_calls=run.had_tool_calls,
                    injected_messages=tuple(injected_messages),
                    stop_by_default=not run.had_tool_calls and not injected_messages,
                )
        finally:
            self._current_scope = None
            with anyio.CancelScope(shield=True):
                await loop_events.aclose()
            if self._current_token is token:
                self._current_token = None
            self._running = False

    def _queued_batch(self, kind: QueueKind) -> tuple[Message, ...]:
        queue = self._queue_for(kind)
        batch = tuple(queue)
        mode = self._config.steering_mode if kind == "steering" else self._config.follow_up_mode
        return batch[:1] if mode == "one_at_a_time" else batch

    def _inject_queued_message(
        self, kind: QueueKind, expected: Message
    ) -> QueueMessageInjected | None:
        queue = self._queue_for(kind)
        if not queue or queue[0] is not expected:
            return None
        message = queue.popleft()
        self._messages.append(message)
        return QueueMessageInjected(
            kind=kind,
            content=message.content,
            skill_invocation=message.skill_invocation,
            timestamp=message.created_at,
        )

    def _apply_transcript_transition(
        self,
        decision: RequestBoundaryDecision,
        *,
        continuation_messages: Sequence[Message],
    ) -> None:
        """Keep harness history atomic with a loop replacement or rebase."""

        if decision.stop:
            return
        if decision.messages is not None:
            self._messages = [*decision.messages, *decision.extra_messages]
            return
        if decision.context_rebase is not None:
            self._messages = [
                *decision.context_rebase.base_messages,
                *continuation_messages,
                *decision.extra_messages,
            ]

    def _ensure_idle(self) -> None:
        if self._running:
            raise RuntimeError(
                "AgentHarness is already running; use steer() or follow_up() to queue messages"
            )

    def _queue_for(self, kind: QueueKind) -> deque[Message]:
        if kind == "steering":
            return self._steering_queue
        if kind == "follow_up":
            return self._follow_up_queue
        raise ValueError(f"Unsupported queue kind: {kind!r}")

    def _require_queue_capacity(self) -> None:
        pending = len(self._steering_queue) + len(self._follow_up_queue)
        maximum = self._config.max_pending_queue_messages
        if pending >= maximum:
            raise RuntimeError(f"Agent queue is full (maximum {maximum} pending messages)")

    @staticmethod
    def _require_user_queue_message(message: Message) -> None:
        if message.role != "user":
            raise ValueError("AgentHarness queues require a user message")


__all__ = [
    "AgentHarness",
    "AgentHarnessConfig",
    "AgentHarnessEvent",
    "HarnessBoundaryContext",
    "HarnessBoundaryPreparer",
    "QueuedMessages",
    "QueueKind",
    "SimpleCancellationToken",
]
