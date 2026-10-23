"""Stateful provider-neutral harness built on the pure agent loop."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum, auto

import anyio

from wisp.agent.loop import AgentLoopEvent, run_agent_loop
from wisp.agent.request_boundary import ContextOverflowHook
from wisp.agent.transcript_repair import plan_interrupted_tool_repairs
from wisp.agent.turn_lifecycle import TurnLifecycle
from wisp.agent.validation import validate_non_negative_integer
from wisp.events import (
    MessageCompleted,
    QueueKind,
    QueueMessageInjected,
    QueueMode,
    QueueUpdated,
    ToolExecutionEnded,
    TurnCompleted,
    TurnStarted,
)
from wisp.messages import (
    Message,
    completion_event_has_history,
    message_from_completion_event,
)

from .boundaries import HarnessBoundaryPreparer, _HarnessBoundaryCoordinator
from .config import AgentHarnessConfig, _build_loop_config
from .queues import QueuedMessages, _QueueStore

type AgentHarnessEvent = AgentLoopEvent | QueueMessageInjected | QueueUpdated


class RunCancellationToken:
    """Cooperative cancellation token owned by one harness run."""

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        """Request cancellation."""
        self._cancelled = True

    def is_cancelled(self) -> bool:
        """Return whether cancellation has been requested."""
        return self._cancelled


@dataclass(slots=True)
class _HarnessRunState:
    """Turn lifecycle and cancellation phase for one primary loop invocation.

    A run is either running or draining a cancellation. It starts draining once a
    loop advance's cancel scope has been cancelled, and never returns to running.
    """

    turns: TurnLifecycle = field(default_factory=TurnLifecycle)
    draining_cancellation: bool = False


class _AdvanceMode(Enum):
    """How one loop advance runs relative to cancellation."""

    # No cancellation requested: cancel() can interrupt the advance via its scope.
    INTERRUPTIBLE = auto()
    # Cancellation requested between advances while a tool batch is open: run one
    # pre-cancelled, unshielded advance so the cancellation reaches the batch.
    DELIVER_CANCELLATION = auto()
    # Cancellation delivered: shield the advance so outstanding tool results settle.
    SETTLE = auto()


class _LoopStepAction(Enum):
    """Control outcomes when advancing the loop produces no event to publish."""

    # Publish any missing cancellation terminals and end the run.
    CANCEL_AND_STOP = auto()
    # The loop finished normally.
    STREAM_ENDED = auto()
    # A cancelled advance produced no event; advance again to keep settling.
    ADVANCE_AGAIN = auto()


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
        self._current_token: RunCancellationToken | None = None
        self._current_scope: anyio.CancelScope | None = None
        self._running = False
        self._queues = _QueueStore()

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
        return self._queues.snapshot()

    @property
    def pending_message_count(self) -> int:
        """Return the total number of pending steering and follow-up messages."""
        return self._queues.count

    @property
    def pending_message_bytes(self) -> int:
        """Return serialized bytes retained across both pending queues."""
        return self._queues.byte_size

    def has_queued_messages(self) -> bool:
        """Return whether either queue contains a pending message."""
        return self._queues.has_messages()

    def replace_config(self, config: AgentHarnessConfig) -> None:
        """Replace provider/tool configuration between runs."""
        self._ensure_idle()
        self._config = config
        # The new configuration may carry different drain modes.
        self._queues.mark_changed()

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
        self._enqueue("steering", message)
        return self.queue_updated_event()

    def follow_up(self, content: str) -> QueueUpdated:
        """Queue user text for injection when a run would otherwise stop."""
        return self.follow_up_message(Message(role="user", content=content))

    def follow_up_message(self, message: Message) -> QueueUpdated:
        """Queue a detached user message for follow-up without changing the transcript."""
        self._enqueue("follow_up", message)
        return self.queue_updated_event()

    def set_steering_mode(self, mode: QueueMode) -> QueueUpdated:
        """Set how many steering messages a future drain will inject."""
        self._config = replace(self._config, steering_mode=mode)
        self._queues.mark_changed()
        return self.queue_updated_event()

    def set_follow_up_mode(self, mode: QueueMode) -> QueueUpdated:
        """Set how many follow-up messages a future drain will inject."""
        self._config = replace(self._config, follow_up_mode=mode)
        self._queues.mark_changed()
        return self.queue_updated_event()

    def pop_latest_steering(self) -> Message | None:
        """Remove and return the latest steering message for editing."""
        return self._queues.pop_latest("steering")

    def pop_latest_follow_up(self) -> Message | None:
        """Remove and return the latest follow-up message for editing."""
        return self._queues.pop_latest("follow_up")

    def clear_queue(self, kind: QueueKind) -> tuple[Message, ...]:
        """Clear one queue and return its previous contents in FIFO order."""
        return self._queues.clear(kind)

    def clear_queues(self) -> QueuedMessages:
        """Clear both queues and return their previous contents."""
        return self._queues.clear_all()

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
        """Return current queues and their opaque revision.

        Returns:
            QueueUpdated: Detached contents, drain modes, and the current owner-state token.
        """
        return self._queues.updated_event(
            steering_mode=self._config.steering_mode,
            follow_up_mode=self._config.follow_up_mode,
        )

    def prompt(
        self,
        content: str,
        *,
        turn_offset: int = 0,
        tool_iteration_offset: int = 0,
        boundary_preparer: HarnessBoundaryPreparer | None = None,
        context_overflow_hook: ContextOverflowHook | None = None,
    ) -> AsyncGenerator[AgentHarnessEvent, None]:
        """Create an event stream for a new user prompt.

        Args:
            content (str): User text to append when the stream is consumed.
            turn_offset (int): Number of turns preceding this run.
            tool_iteration_offset (int): Number of earlier tool iterations.
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
            boundary_preparer=boundary_preparer,
            context_overflow_hook=context_overflow_hook,
        )

    def prompt_message(
        self,
        message: Message,
        *,
        turn_offset: int = 0,
        tool_iteration_offset: int = 0,
        boundary_preparer: HarnessBoundaryPreparer | None = None,
        context_overflow_hook: ContextOverflowHook | None = None,
    ) -> AsyncGenerator[AgentHarnessEvent, None]:
        """Create a run from an existing user message, preserving its metadata.

        Args:
            message (Message): User message to snapshot now and append when
                iteration begins. Later caller mutations do not affect the run.
            turn_offset (int): Number of turns preceding this run.
            tool_iteration_offset (int): Number of earlier tool iterations.
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
            boundary_preparer=boundary_preparer,
            context_overflow_hook=context_overflow_hook,
        )

    def continue_(
        self,
        *,
        turn_offset: int = 0,
        tool_iteration_offset: int = 0,
        boundary_preparer: HarnessBoundaryPreparer | None = None,
        context_overflow_hook: ContextOverflowHook | None = None,
    ) -> AsyncGenerator[AgentHarnessEvent, None]:
        """Continue from the current transcript without adding a user message.

        Args:
            turn_offset (int): Number of turns preceding this continuation.
            tool_iteration_offset (int): Number of earlier tool iterations.
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
            boundary_preparer=boundary_preparer,
            context_overflow_hook=context_overflow_hook,
        )

    async def _run(
        self,
        *,
        prompt_message: Message | None = None,
        turn_offset: int = 0,
        tool_iteration_offset: int = 0,
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
        token = RunCancellationToken()
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
                request_boundary_hook=boundary,
                context_overflow_hook=boundary if context_overflow_hook is not None else None,
            )
            loop_events = run_agent_loop(config, messages=boundary.provider_history())
            while True:
                step = await self._next_loop_step(loop_events, token=token, run=run)
                if step is _LoopStepAction.CANCEL_AND_STOP:
                    for cancellation_event in run.turns.cancelled_events():
                        yield cancellation_event
                    return
                if step is _LoopStepAction.STREAM_ENDED:
                    break
                if step is _LoopStepAction.ADVANCE_AGAIN:
                    continue

                event = step
                run.turns.observe(event)
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
                if self._queues.has_messages("steering"):
                    queue_kind = "steering"
                elif not run.turns.had_tool_calls and self._queues.has_messages("follow_up"):
                    queue_kind = "follow_up"

                injected_messages: list[Message] = []
                if queue_kind is not None:
                    drain_batch = self._queued_batch(queue_kind)
                    for message in drain_batch:
                        if token.is_cancelled():
                            for cancellation_event in run.turns.cancelled_events():
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
                        for cancellation_event in run.turns.cancelled_events():
                            yield cancellation_event
                        return

                boundary.arm(
                    turn=event.turn,
                    had_tool_calls=run.turns.had_tool_calls,
                    injected_messages=injected_messages,
                    stop_by_default=not run.turns.had_tool_calls and not injected_messages,
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
        token: RunCancellationToken,
        run: _HarnessRunState,
    ) -> AgentLoopEvent | _LoopStepAction:
        """Advance the loop once under the cancellation rules for this point in the run.

        Each advance runs in the `_AdvanceMode` chosen from the run's phase and the token:

        | Phase    | Token     | Open tool batch | Advance                                   |
        | -------- | --------- | --------------- | ----------------------------------------- |
        | running  | clear     | any             | INTERRUPTIBLE                             |
        | running  | cancelled | yes             | DELIVER_CANCELLATION                      |
        | running  | cancelled | no              | none; return CANCEL_AND_STOP immediately  |
        | draining | cancelled | any             | SETTLE                                    |

        The run starts draining when the advance's scope was cancelled, either by
        `cancel()` during an interruptible advance or by a delivering advance. When the
        loop publishes cancellation terminals itself, `_run` observes them and returns;
        otherwise CANCEL_AND_STOP makes `_run` publish the missing ones. The scope exits
        before returning, leaving retention, publication, and final cleanup to `_run`.

        Args:
            loop_events (AsyncGenerator[AgentLoopEvent, None]): Owned primary loop stream.
            token (RunCancellationToken): Cancellation state for this invocation.
            run (_HarnessRunState): Observed turn state and mutable cancellation phase.

        Returns:
            AgentLoopEvent | _LoopStepAction: An event to retain and publish, or an
            action to advance again, stop normally, or emit cancellation terminals.

        Raises:
            RuntimeError: Advancement produced no event outside cancellation draining.
            Exception: Loop advancement failures propagate to the caller's cleanup.
        """
        mode = self._advance_mode(token, run)
        if mode is None:
            return _LoopStepAction.CANCEL_AND_STOP

        # A delivering advance stays unshielded so its own cancellation reaches the batch.
        scope = anyio.CancelScope(shield=mode is _AdvanceMode.SETTLE)
        # Once cancellation is being delivered, a later cancel() only sets the token.
        self._current_scope = scope if mode is _AdvanceMode.INTERRUPTIBLE else None
        event: AgentLoopEvent | None = None
        stream_ended = False
        with scope:
            if mode is _AdvanceMode.DELIVER_CANCELLATION:
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
                return _LoopStepAction.ADVANCE_AGAIN
            raise RuntimeError("Agent loop produced no event")
        return event

    @staticmethod
    def _advance_mode(token: RunCancellationToken, run: _HarnessRunState) -> _AdvanceMode | None:
        """Choose how the next loop advance runs.

        Args:
            token (RunCancellationToken): Cancellation state for this invocation.
            run (_HarnessRunState): Observed turn state and cancellation phase.

        Returns:
            _AdvanceMode | None: The advance mode, or None when cancellation was
                requested and nothing is in flight to settle, so the run stops
                without advancing the loop.
        """
        if not token.is_cancelled():
            return _AdvanceMode.INTERRUPTIBLE
        if run.draining_cancellation:
            return _AdvanceMode.SETTLE
        if run.turns.open_turn is not None and run.turns.had_tool_calls:
            return _AdvanceMode.DELIVER_CANCELLATION
        return None

    def _enqueue(self, kind: QueueKind, message: Message) -> None:
        self._queues.enqueue(
            kind,
            message,
            max_messages=self._config.max_pending_queue_messages,
            max_bytes=self._config.max_pending_queue_bytes,
        )

    def _queued_batch(self, kind: QueueKind) -> tuple[Message, ...]:
        mode = self._config.steering_mode if kind == "steering" else self._config.follow_up_mode
        return self._queues.next_batch(kind, mode)

    def _inject_queued_message(
        self, kind: QueueKind, expected: Message
    ) -> QueueMessageInjected | None:
        message = self._queues.take_next(kind, expected)
        if message is None:
            return None
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
