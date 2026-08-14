"""Stateful provider-neutral harness built on the pure agent loop."""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, replace
from enum import Enum, auto

import anyio

from wisp.agent.configuration import validate_agent_runtime_limits
from wisp.agent.execution import ToolExecutor
from wisp.agent.loop import AgentLoopConfig, AgentLoopEvent, UsageCostEstimator, run_agent_loop
from wisp.agent.messages import (
    Message,
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
    """Offsets and active-turn state retained across loop segments."""

    next_turn_offset: int
    next_tool_iteration_offset: int
    active_turn: int | None = None
    active_turn_completed: bool = False

    def cancelled_events(self) -> tuple[AgentLoopEvent, ...]:
        return _cancelled_events(
            self.active_turn,
            active_turn_completed=self.active_turn_completed,
        )


@dataclass(slots=True)
class _HarnessSegmentState:
    """Observable completion state for one invocation of the pure loop."""

    run_finished: bool = False
    had_tool_calls: bool = False
    outcome: str | None = None
    stream_ended: bool = False
    restart_for_steering: bool = False

    def observe(self, event: AgentLoopEvent, run: _HarnessRunState) -> None:
        if isinstance(event, TurnStarted):
            run.active_turn = event.turn
            run.active_turn_completed = False
            run.next_turn_offset = event.turn
            self.run_finished = False
        if isinstance(event, MessageCompleted):
            self.run_finished = not event.tool_calls
            if event.tool_calls:
                self.had_tool_calls = True
                run.next_tool_iteration_offset += 1
        elif isinstance(event, TurnCompleted):
            run.active_turn_completed = True
            self.outcome = event.outcome
            self.run_finished = self.run_finished or event.outcome != "completed"

    def can_drain_follow_up(self, *, cancelled: bool) -> bool:
        return (
            self.stream_ended
            and not cancelled
            and self.outcome == "completed"
            and self.run_finished
        )


class _SegmentDisposition(Enum):
    """Next harness action after a completed loop segment."""

    RESTART_FOR_STEERING = auto()
    DRAIN_FOLLOW_UP = auto()
    FINISH = auto()


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
    ) -> AsyncGenerator[AgentHarnessEvent, None]:
        """Append a user message and start a run."""
        return self.prompt_message(
            Message(role="user", content=content),
            turn_offset=turn_offset,
            tool_iteration_offset=tool_iteration_offset,
            defer_context_overflow_errors=defer_context_overflow_errors,
        )

    def prompt_message(
        self,
        message: Message,
        *,
        turn_offset: int = 0,
        tool_iteration_offset: int = 0,
        defer_context_overflow_errors: bool = False,
    ) -> AsyncGenerator[AgentHarnessEvent, None]:
        """Append an existing user message and start a run."""
        if message.role != "user":
            raise ValueError("AgentHarness prompts require a user message")
        return self._run(
            prompt_message=message,
            turn_offset=turn_offset,
            tool_iteration_offset=tool_iteration_offset,
            defer_context_overflow_errors=defer_context_overflow_errors,
        )

    def continue_(
        self,
        *,
        turn_offset: int = 0,
        tool_iteration_offset: int = 0,
        defer_context_overflow_errors: bool = False,
        pause_after_tool_round: bool = False,
    ) -> AsyncGenerator[AgentHarnessEvent, None]:
        """Continue from the current transcript without adding a user message."""
        return self._run(
            turn_offset=turn_offset,
            tool_iteration_offset=tool_iteration_offset,
            defer_context_overflow_errors=defer_context_overflow_errors,
            pause_after_tool_round=pause_after_tool_round,
        )

    async def _run(
        self,
        *,
        prompt_message: Message | None = None,
        turn_offset: int = 0,
        tool_iteration_offset: int = 0,
        defer_context_overflow_errors: bool = False,
        pause_after_tool_round: bool = False,
    ) -> AsyncGenerator[AgentHarnessEvent, None]:
        self.repair_interrupted_tool_calls()
        self._running = True
        token = SimpleCancellationToken()
        self._current_token = token
        if prompt_message is not None:
            self._messages.append(prompt_message)

        run = _HarnessRunState(
            next_turn_offset=turn_offset,
            next_tool_iteration_offset=tool_iteration_offset,
        )
        try:
            while True:
                segment = _HarnessSegmentState()
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
                    turn_offset=run.next_turn_offset,
                    tool_iteration_offset=run.next_tool_iteration_offset,
                    cost_estimator=self._config.cost_estimator,
                    defer_context_overflow_errors=defer_context_overflow_errors,
                )
                provider_messages = normalize_provider_history(
                    self._messages, active_from=_active_turn_start(self._messages)
                )
                loop_events = run_agent_loop(config, messages=provider_messages)
                try:
                    while True:
                        if token.is_cancelled() and not segment.run_finished:
                            for cancellation_event in run.cancelled_events():
                                yield cancellation_event
                            return

                        scope = anyio.CancelScope()
                        self._current_scope = scope
                        event: AgentLoopEvent | None = None
                        with scope:
                            try:
                                event = await anext(loop_events)
                            except StopAsyncIteration:
                                segment.stream_ended = True
                        if self._current_scope is scope:
                            self._current_scope = None

                        if scope.cancel_called:
                            if not segment.run_finished:
                                for cancellation_event in run.cancelled_events():
                                    yield cancellation_event
                            return
                        if segment.stream_ended:
                            break
                        assert event is not None

                        segment.observe(event, run)
                        if isinstance(event, MessageCompleted | ToolExecutionEnded):
                            # ToolResultReady copies the terminal tool payload; retain it now
                            # so closing at this visible boundary cannot lose output.
                            self._messages.append(message_from_completion_event(event))
                        yield event
                        if (
                            pause_after_tool_round
                            and isinstance(event, TurnCompleted)
                            and event.outcome == "completed"
                            and segment.had_tool_calls
                            and not self._steering_queue
                        ):
                            return
                        if (
                            isinstance(event, TurnCompleted)
                            and event.outcome == "completed"
                            and self._steering_queue
                        ):
                            segment.restart_for_steering = True
                            break
                finally:
                    self._current_scope = None
                    with anyio.CancelScope(shield=True):
                        await loop_events.aclose()

                disposition = self._segment_disposition(segment, cancelled=token.is_cancelled())
                if disposition is _SegmentDisposition.RESTART_FOR_STEERING:
                    if token.is_cancelled():
                        for cancellation_event in run.cancelled_events():
                            yield cancellation_event
                        return
                    drain_batch = self._queued_batch("steering")
                    for message in drain_batch:
                        if token.is_cancelled():
                            for cancellation_event in run.cancelled_events():
                                yield cancellation_event
                            return
                        injected_event = self._inject_queued_message("steering", message)
                        if injected_event is not None:
                            yield injected_event
                    yield self.queue_updated_event()
                    if pause_after_tool_round and segment.had_tool_calls:
                        return
                    continue

                if disposition is _SegmentDisposition.FINISH:
                    break

                drain_batch = self._queued_batch("follow_up")
                if not drain_batch:
                    break

                for message in drain_batch:
                    if token.is_cancelled():
                        for cancellation_event in run.cancelled_events():
                            yield cancellation_event
                        return
                    injected_event = self._inject_queued_message("follow_up", message)
                    if injected_event is not None:
                        yield injected_event
                yield self.queue_updated_event()
        finally:
            self._current_scope = None
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

    @staticmethod
    def _segment_disposition(
        segment: _HarnessSegmentState, *, cancelled: bool
    ) -> _SegmentDisposition:
        if segment.restart_for_steering:
            return _SegmentDisposition.RESTART_FOR_STEERING
        if segment.can_drain_follow_up(cancelled=cancelled):
            return _SegmentDisposition.DRAIN_FOLLOW_UP
        return _SegmentDisposition.FINISH

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
    "QueuedMessages",
    "QueueKind",
    "SimpleCancellationToken",
]
