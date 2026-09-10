"""Stateful provider-neutral harness built on the pure agent loop."""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, replace
from enum import Enum, auto

import anyio

from wisp.agent.loop import AgentLoopEvent, run_agent_loop
from wisp.agent.messages import (
    Message,
    completion_event_has_history,
    message_from_completion_event,
)
from wisp.agent.request_boundary import ContextOverflowHook
from wisp.agent.transcript_repair import plan_interrupted_tool_repairs
from wisp.agent.validation import validate_non_negative_integer
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
from wisp.providers.base import prepare_provider_history

from .boundaries import HarnessBoundaryPreparer, _HarnessBoundaryCoordinator
from .config import AgentHarnessConfig, _build_loop_config

type AgentHarnessEvent = AgentLoopEvent | QueueMessageInjected | QueueUpdated


@dataclass(frozen=True, slots=True)
class QueuedMessages:
    """Detached snapshot of harness-owned queued user messages."""

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
    """Turn lifecycle and cancellation drain state for one primary loop invocation."""

    active_turn: int | None = None
    active_turn_completed: bool = False
    had_tool_calls: bool = False
    draining_cancellation: bool = False

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


class _LoopStepAction(Enum):
    """Control outcomes when advancing the loop produces no event to publish."""

    CANCEL_AND_STOP = auto()
    STREAM_ENDED = auto()
    RETRY = auto()


class AgentHarness:
    """Own an in-memory transcript and delegate execution to the pure loop."""

    def __init__(
        self,
        config: AgentHarnessConfig,
        *,
        messages: Sequence[Message] = (),
    ) -> None:
        self._config = config
        self._messages = [message.model_copy(deep=True) for message in messages]
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
        """Return a detached transcript snapshot, including nested message data."""
        return tuple(message.model_copy(deep=True) for message in self._messages)

    @property
    def is_running(self) -> bool:
        """Return whether a prompt or continuation is active."""
        return self._running

    @property
    def queued_messages(self) -> QueuedMessages:
        """Return detached snapshots of both pending queues and their nested data."""
        return QueuedMessages(
            steering=tuple(message.model_copy(deep=True) for message in self._steering_queue),
            follow_up=tuple(message.model_copy(deep=True) for message in self._follow_up_queue),
        )

    @property
    def pending_message_count(self) -> int:
        """Return the total number of pending steering and follow-up messages."""
        return len(self._steering_queue) + len(self._follow_up_queue)

    @property
    def pending_message_bytes(self) -> int:
        """Return serialized bytes retained across both pending queues."""

        return sum(
            self._queued_message_size(message)
            for message in (*self._steering_queue, *self._follow_up_queue)
        )

    def has_queued_messages(self) -> bool:
        """Return whether either queue contains a pending message."""
        return bool(self._steering_queue or self._follow_up_queue)

    def replace_config(self, config: AgentHarnessConfig) -> None:
        """Replace provider/tool configuration between runs."""
        self._ensure_idle()
        self._config = config

    def append_message(self, message: Message) -> None:
        """Append a detached copy of restored or application-provided state."""
        self._ensure_idle()
        self._messages.append(message.model_copy(deep=True))

    def replace_messages(self, messages: Sequence[Message]) -> None:
        """Replace the transcript with detached copies between runs."""
        self._ensure_idle()
        self._messages = [message.model_copy(deep=True) for message in messages]

    def repair_interrupted_tool_calls(self) -> tuple[Message, ...]:
        """Repair logical ordering and return synthetic results needing persistence."""

        self._ensure_idle()
        plan = plan_interrupted_tool_repairs(self._messages)
        self._messages = list(plan.messages)
        return tuple(message.model_copy(deep=True) for message in plan.repairs)

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
        """Queue a detached user message for steering without changing the transcript."""
        self._require_user_queue_message(message)
        self._require_queue_capacity(message)
        self._steering_queue.append(message.model_copy(deep=True))
        return self.queue_updated_event()

    def follow_up(self, content: str) -> QueueUpdated:
        """Queue user text for injection when a run would otherwise stop."""
        return self.follow_up_message(Message(role="user", content=content))

    def follow_up_message(self, message: Message) -> QueueUpdated:
        """Queue a detached user message for follow-up without changing the transcript."""
        self._require_user_queue_message(message)
        self._require_queue_capacity(message)
        self._follow_up_queue.append(message.model_copy(deep=True))
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
        """Create an event stream for a new user prompt.

        Args:
            content (str): User text to append when the stream is consumed.
            turn_offset (int): Number of turns preceding this run.
            tool_iteration_offset (int): Number of earlier tool iterations.
            defer_context_overflow_errors (bool): Whether overflow recovery is
                delegated to the enclosing session.
            boundary_preparer (HarnessBoundaryPreparer | None): Session policy
                invoked between completed turns and new provider requests.
            context_overflow_hook (ContextOverflowHook | None): Recovery hook for
                provider-rejected context.

        Returns:
            AsyncGenerator[AgentHarnessEvent, None]: Lazy stream of loop and queue
            events. Consume or close it to allow the harness to finish cleanup.
        """
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
        """Create a run from an existing user message, preserving its metadata.

        Args:
            message (Message): User message to snapshot now and append when
                iteration begins. Later caller mutations do not affect the run.
            turn_offset (int): Number of turns preceding this run.
            tool_iteration_offset (int): Number of earlier tool iterations.
            defer_context_overflow_errors (bool): Whether to defer overflow errors.
            boundary_preparer (HarnessBoundaryPreparer | None): Session policy
                invoked before subsequent requests.
            context_overflow_hook (ContextOverflowHook | None): Provider-overflow
                recovery hook.

        Returns:
            AsyncGenerator[AgentHarnessEvent, None]: Lazy stream with the same
            lifecycle and queue behavior as prompt().

        Raises:
            ValueError: If the supplied message does not have the user role.
        """
        if message.role != "user":
            raise ValueError("AgentHarness prompts require a user message")
        return self._run(
            prompt_message=message.model_copy(deep=True),
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
        """Continue from the current transcript without adding a user message.

        Args:
            turn_offset (int): Number of turns preceding this continuation.
            tool_iteration_offset (int): Number of earlier tool iterations.
            defer_context_overflow_errors (bool): Whether to defer overflow errors.
            boundary_preparer (HarnessBoundaryPreparer | None): Session policy
                invoked before subsequent requests.
            context_overflow_hook (ContextOverflowHook | None): Provider-overflow
                recovery hook.

        Returns:
            AsyncGenerator[AgentHarnessEvent, None]: Lazy event stream that repairs
            interrupted tool calls before running and releases run state on close.
        """
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
        """Stream one invocation while retaining conversation and queue state.

        Validate offsets before repairing history or accepting a prompt. Consume
        the loop, retaining completed messages before yielding their events; after
        each successful turn, drain the eligible queue and arm the next boundary.
        Apply transcript replacements only when the next turn starts. Always
        release run state, including when startup or stream cleanup fails.

        Args:
            prompt_message (Message | None): Detached prompt to append when consumed.
            turn_offset (int): Number of turns preceding this invocation.
            tool_iteration_offset (int): Number of earlier tool iterations.
            defer_context_overflow_errors (bool): Whether the session handles overflow errors.
            boundary_preparer (HarnessBoundaryPreparer | None): Session-owned boundary policy.
            context_overflow_hook (ContextOverflowHook | None): Optional overflow recovery.

        Yields:
            AgentHarnessEvent: Loop and queue events in transcript publication order.
                Consume or close the stream to release the guarded run lifetime.

        Raises:
            RuntimeError: Another invocation is active.
            ValueError: An invocation offset is invalid.
            Exception: Startup, execution, boundary, or cleanup failures propagate
                without rolling back accepted input or retained completions.
        """
        self._ensure_idle()
        validate_non_negative_integer(turn_offset, field="turn_offset")
        validate_non_negative_integer(tool_iteration_offset, field="tool_iteration_offset")
        self.repair_interrupted_tool_calls()
        # Every row already present when a run begins is durable history. New
        # assistant/tool rows appended during this invocation form the only live
        # tail that may need native reconstruction at an internal request boundary.
        boundary = _HarnessBoundaryCoordinator(
            get_messages=lambda: self.messages,
            provider=self._config.provider,
            effort=self._config.effort,
            active_from=len(self._messages),
            boundary_preparer=boundary_preparer,
            context_overflow_hook=context_overflow_hook,
        )
        token = SimpleCancellationToken()
        run = _HarnessRunState()
        loop_events: AsyncGenerator[AgentLoopEvent, None] | None = None
        self._running = True
        self._current_token = token
        try:
            if prompt_message is not None:
                self._messages.append(prompt_message)
            config = _build_loop_config(
                self._config,
                cancellation_token=token,
                turn_offset=turn_offset,
                tool_iteration_offset=tool_iteration_offset,
                defer_context_overflow_errors=defer_context_overflow_errors,
                request_boundary_hook=boundary,
                context_overflow_hook=boundary if context_overflow_hook is not None else None,
            )
            provider_messages = prepare_provider_history(
                self.messages,
                provider=self._config.provider,
                effort=self._config.effort,
                active_from=boundary.active_from,
            )
            loop_events = run_agent_loop(config, messages=provider_messages)
            while True:
                step = await self._next_loop_step(loop_events, token=token, run=run)
                if step is _LoopStepAction.CANCEL_AND_STOP:
                    for cancellation_event in run.cancelled_events():
                        yield cancellation_event
                    return
                if step is _LoopStepAction.STREAM_ENDED:
                    break
                if step is _LoopStepAction.RETRY:
                    continue

                event = step
                run.observe(event)
                if isinstance(event, TurnStarted):
                    replacement = boundary.take_transcript_replacement()
                    if replacement is not None:
                        self._messages = [message.model_copy(deep=True) for message in replacement]
                        # The accepted request consumed these rows. Only later
                        # completions belong to the next boundary's active tail.
                        boundary.active_from = len(self._messages)
                if isinstance(
                    event, MessageCompleted | ToolExecutionEnded
                ) and completion_event_has_history(event):
                    # ToolResultReady copies the terminal tool payload; retain it now
                    # so closing at this visible boundary cannot lose output. Empty
                    # failed assistant completions settle lifecycle state only.
                    self._messages.append(message_from_completion_event(event))
                yield event

                if isinstance(event, TurnCompleted) and event.outcome == "cancelled":
                    return
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

                boundary.arm(
                    turn=event.turn,
                    had_tool_calls=run.had_tool_calls,
                    injected_messages=injected_messages,
                    stop_by_default=not run.had_tool_calls and not injected_messages,
                )
        finally:
            self._current_scope = None
            try:
                if loop_events is not None:
                    with anyio.CancelScope(shield=True):
                        await loop_events.aclose()
            finally:
                if self._current_token is token:
                    self._current_token = None
                self._running = False

    async def _next_loop_step(
        self,
        loop_events: AsyncGenerator[AgentLoopEvent, None],
        *,
        token: SimpleCancellationToken,
        run: _HarnessRunState,
    ) -> AgentLoopEvent | _LoopStepAction:
        """Advance the loop once, interrupting or draining cancellation as needed.

        The first cancellation interrupts an unshielded advance; later advances
        are shielded so outstanding tool results can settle. Draining lasts for
        the invocation, independently of per-turn state. The scope exits before
        returning, leaving retention, publication, and final cleanup to _run.

        Args:
            loop_events (AsyncGenerator[AgentLoopEvent, None]): Owned primary loop stream.
            token (SimpleCancellationToken): Cancellation state for this invocation.
            run (_HarnessRunState): Observed turn state and mutable cancellation drain state.

        Returns:
            AgentLoopEvent | _LoopStepAction: An event to retain and publish, or an
            action to retry advancement, stop normally, or emit cancellation terminals.

        Raises:
            RuntimeError: Advancement produced no event outside cancellation draining.
            Exception: Loop advancement failures propagate to the caller's cleanup.
        """
        if (
            token.is_cancelled()
            and not run.draining_cancellation
            and (not run.had_tool_calls or run.active_turn_completed)
        ):
            # A completed tool turn has no outstanding batch to settle.
            return _LoopStepAction.CANCEL_AND_STOP
        start_cancellation = token.is_cancelled() and not run.draining_cancellation
        if start_cancellation:
            run.draining_cancellation = True

        scope = anyio.CancelScope(shield=run.draining_cancellation and not start_cancellation)
        self._current_scope = None if run.draining_cancellation else scope
        event: AgentLoopEvent | None = None
        stream_ended = False
        with scope:
            if start_cancellation:
                scope.cancel()
            try:
                event = await anext(loop_events)
            except StopAsyncIteration:
                stream_ended = True
        if self._current_scope is scope:
            self._current_scope = None

        if scope.cancel_called:
            run.draining_cancellation = True
        if stream_ended:
            if run.draining_cancellation:
                return _LoopStepAction.CANCEL_AND_STOP
            return _LoopStepAction.STREAM_ENDED
        if event is None:
            if run.draining_cancellation:
                return _LoopStepAction.RETRY
            raise RuntimeError("Agent loop produced no event")
        return event

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

    def _require_queue_capacity(self, message: Message) -> None:
        pending = len(self._steering_queue) + len(self._follow_up_queue)
        maximum = self._config.max_pending_queue_messages
        if pending >= maximum:
            raise RuntimeError(f"Agent queue is full (maximum {maximum} pending messages)")
        pending_bytes = self.pending_message_bytes
        message_bytes = self._queued_message_size(message)
        maximum_bytes = self._config.max_pending_queue_bytes
        if message_bytes > maximum_bytes - pending_bytes:
            raise RuntimeError(
                f"Agent queue byte limit exceeded (maximum {maximum_bytes} pending bytes)"
            )

    @staticmethod
    def _queued_message_size(message: Message) -> int:
        return len(message.model_dump_json().encode("utf-8"))

    @staticmethod
    def _require_user_queue_message(message: Message) -> None:
        if message.role != "user":
            raise ValueError("AgentHarness queues require a user message")
