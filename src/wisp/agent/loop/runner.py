"""Pure provider and tool-call agent loop."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field

from wisp.agent.context_budget import estimate_context_budget
from wisp.agent.messages import Message
from wisp.events import (
    ContextEstimated,
    ContextOverflow,
    ContextPressure,
    ErrorEvent,
    MessageCompleted,
    MessageDelta,
    MessageStarted,
    ProviderRetrying,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolExecutionEnded,
    ToolExecutionStarted,
    ToolResultReady,
    TurnCompleted,
    TurnStarted,
)
from wisp.providers.base import (
    ContextOverflowError,
    is_context_overflow_message,
)

from .config import AgentLoopConfig
from .continuation import (
    ContinuationState,
    at_context_overflow,
    at_request_boundary,
)
from .model_response import (
    CancelledModelResponse,
    CompletedModelResponse,
    FailedModelResponse,
    ModelResponseStream,
    OverflowModelResponse,
    project_completed_response,
)
from .tool_execution import CancelledToolBatch, ToolBatch

type AgentLoopEvent = (
    TurnStarted
    | ContextEstimated
    | ProviderRetrying
    | MessageStarted
    | MessageDelta
    | MessageCompleted
    | ContextPressure
    | ContextOverflow
    | ToolCallRequested
    | ToolExecutionStarted
    | ToolApprovalRequested
    | ToolApprovalResolved
    | ToolExecutionEnded
    | ToolResultReady
    | TurnCompleted
    | ErrorEvent
)


def _is_cancelled(config: AgentLoopConfig) -> bool:
    """Read the optional cooperative cancellation token.

    Args:
        config (AgentLoopConfig): Configuration for the current run.

    Returns:
        bool: True if the token reports cancellation; False if no token is configured.
    """
    token = config.cancellation_token
    return token is not None and token.is_cancelled()


def _cancelled_turn_events(turn: int) -> tuple[ErrorEvent, TurnCompleted]:
    """Build terminal cancellation events for a turn that has already started.

    Args:
        turn (int): Number of the active turn.

    Returns:
        tuple[ErrorEvent, TurnCompleted]: Cancellation error followed by turn completion.
    """
    return (
        ErrorEvent(message="Agent run cancelled"),
        TurnCompleted(turn=turn, outcome="cancelled", finish_reason="cancelled"),
    )


@dataclass(slots=True)
class _AgentLoopState:
    """Turn counters plus the extracted continuation cursor/transcript."""

    turn: int
    tool_iterations: int
    continuation: ContinuationState = field(default_factory=ContinuationState)

    def begin_turn(self) -> int:
        """Advance the provider-request counter.

        Returns:
            int: Number assigned to the new turn.
        """
        self.turn += 1
        return self.turn

    def begin_tool_round(self, maximum: int | None) -> None:
        """Reserve one tool iteration before executing a batch.

        Args:
            maximum (int | None): Total allowed batches, or None for no limit.

        Raises:
            RuntimeError: The existing count has already reached the limit.
        """
        if maximum is not None and self.tool_iterations >= maximum:
            raise RuntimeError(f"Maximum tool iterations exceeded: {maximum}")
        self.tool_iterations += 1


async def run_agent_loop(
    config: AgentLoopConfig,
    *,
    messages: Sequence[Message],
) -> AsyncGenerator[AgentLoopEvent, None]:
    """Stream model responses and tool batches until the run stops.

    Each turn contains one model response and its requested tool batch, if any.
    Without a boundary hook, a response with tools continues and a response without
    tools ends the run. Hooks can inject messages or replace/rebase context between
    requests; persistence and frontend behavior belong to the caller.

    Args:
        config (AgentLoopConfig): Provider, executor, limits, and optional policy hooks.
        messages (Sequence[Message]): Base history for the first request. Continuation
            is tracked separately; this sequence is not appended to by the loop.

    Yields:
        AgentLoopEvent: Ordered turn, context, message, tool, and error events.
            MessageCompleted precedes tool execution; TurnCompleted ends one turn,
            not necessarily the run.

    Examples:
        Collect completed messages using application-supplied configuration and history::

            from wisp.agent.loop import run_agent_loop
            from wisp.events import MessageCompleted

            async def completed_messages(config, history):
                messages = []
                async for event in run_agent_loop(config, messages=history):
                    if isinstance(event, MessageCompleted):
                        messages.append(event)
                return messages

    Raises:
        ContextOverflowError: A raised provider overflow has no recovery hook, or
            overflow handling itself raises. Deferred errors can still re-raise.
        Exception: Unexpected provider, executor, token, or hook failures propagate
            after error events. Typed provider failures normally end the stream instead.
    """

    state = _AgentLoopState(
        turn=config.turn_offset,
        tool_iterations=config.tool_iteration_offset,
    )
    # Bind `turn`/`turn_started` before the loop so the outer `except` below
    # can always reference them, even if an exception (e.g. a raising
    # CancellationToken) fires before the first `state.begin_turn()` call.
    # `turn_started` -- rather than `turn > 0` -- distinguishes "no turn
    # started this invocation" from "a real turn is in flight", since a
    # nonzero `turn_offset` would otherwise make `turn > 0` true even when
    # this call never emitted a matching `TurnStarted`.
    turn = config.turn_offset
    turn_started = False

    try:
        while True:
            turn_started = False
            if _is_cancelled(config):
                yield ErrorEvent(message="Agent run cancelled")
                break
            turn = state.begin_turn()
            turn_started = True
            yield TurnStarted(turn=turn)
            if _is_cancelled(config):
                for event in _cancelled_turn_events(turn):
                    yield event
                break
            request_messages = (*messages, *state.continuation.continuation_messages)
            selected_model = config.model or config.provider.default_model
            previous_observation = next(
                (
                    message.context_observation
                    for message in reversed(request_messages)
                    if message.context_observation is not None
                ),
                None,
            )
            context_budget = estimate_context_budget(
                request_messages,
                config.tools,
                context_window=config.context_window,
                reserve_tokens=config.context_reserve_tokens,
                observation=previous_observation,
                provider=config.provider.name,
                model=selected_model,
            )
            yield ContextEstimated(
                turn=turn,
                provider=config.provider.name,
                model=selected_model,
                budget=context_budget,
            )

            response_stream = ModelResponseStream(
                config,
                messages=messages,
                tool_results=state.continuation.pending_tool_results,
                extra_messages=state.continuation.pending_extra_messages,
                previous_response_id=state.continuation.previous_response_id,
            )
            async for provider_event in response_stream.events(turn=turn):
                yield provider_event
            outcome = response_stream.outcome
            if outcome is None:
                raise RuntimeError("Provider turn ended without a typed outcome")
            if isinstance(outcome, CancelledModelResponse):
                return

            if isinstance(outcome, OverflowModelResponse | FailedModelResponse):
                if isinstance(outcome, OverflowModelResponse):
                    failure_message = str(outcome.error)
                    failure_kind = "context_overflow"
                    failure_content = outcome.content
                    failure_response_id = outcome.started_response_id
                else:
                    failure = outcome.failed.response
                    failure_message = failure.message
                    failure_kind = (
                        "context_overflow"
                        if failure.failure_kind == "context_overflow"
                        or is_context_overflow_message(failure.message)
                        else failure.failure_kind
                    )
                    failure_content = outcome.failed.content
                    failure_response_id = outcome.failed.response_id
                # Close an opened message before recovery can start another turn.
                if outcome.started:
                    yield MessageCompleted(
                        turn=turn,
                        content=failure_content,
                        finish_reason="error",
                        response_id=failure_response_id,
                    )
                # Raised overflow without a hook retains its exception contract;
                # the outer handler owns its terminal events and re-raises.
                if (
                    isinstance(outcome, OverflowModelResponse)
                    and config.context_overflow_hook is None
                ):
                    raise outcome.error
                if failure_kind == "context_overflow":
                    yield ContextOverflow(
                        turn=turn,
                        provider=config.provider.name,
                        model=config.model or config.provider.default_model,
                        context_window=config.context_window,
                        message=failure_message,
                    )
                    messages, retry = await at_context_overflow(
                        config,
                        state.continuation,
                        turn=state.turn,
                        tool_iterations=state.tool_iterations,
                        messages=messages,
                        context_budget=context_budget,
                        had_streamed_delta=outcome.had_streamed_delta,
                        message=failure_message,
                    )
                    if retry:
                        yield TurnCompleted(turn=turn, outcome="failed", finish_reason="error")
                        turn_started = False
                        continue
                    if config.defer_context_overflow_errors:
                        return
                yield ErrorEvent(message=failure_message)
                yield TurnCompleted(
                    turn=turn,
                    outcome="cancelled" if failure_kind == "aborted" else "failed",
                    finish_reason="cancelled" if failure_kind == "aborted" else "error",
                )
                return
            if not isinstance(outcome, CompletedModelResponse):
                raise RuntimeError("Provider turn ended without a completed response")
            completed = outcome.completed
            response = completed.response
            tool_calls = response.tool_calls
            projection = project_completed_response(
                config,
                completed,
                turn=turn,
                request_messages=request_messages,
                selected_model=selected_model,
            )
            yield projection.event
            state.continuation.record_response(
                projection.to_continuation_message(), response_id=completed.response_id
            )
            usage = projection.event.usage
            if usage is not None and config.context_window is not None:
                pressure_ratio = usage.input_tokens / config.context_window
                if pressure_ratio >= config.context_pressure_threshold:
                    yield ContextPressure(
                        turn=turn,
                        provider=config.provider.name,
                        model=config.model or config.provider.default_model,
                        context_window=config.context_window,
                        observed_tokens=usage.input_tokens,
                        remaining_tokens=max(0, config.context_window - usage.input_tokens),
                        pressure_ratio=pressure_ratio,
                    )

            had_tool_calls = bool(tool_calls)
            if had_tool_calls:
                if _is_cancelled(config):
                    for event in _cancelled_turn_events(turn):
                        yield event
                    break
                state.begin_tool_round(config.max_tool_iterations)
                tool_batch = ToolBatch(
                    tool_executor=config.tool_executor,
                    tool_calls=tool_calls,
                    truncated=response.finish_reason == "length",
                    is_cancelled=lambda: _is_cancelled(config),
                    on_result=state.continuation.record_tool_result,
                )
                async for tool_event in tool_batch.events():
                    yield tool_event
                tool_outcome = tool_batch.outcome
                if tool_outcome is None:
                    raise RuntimeError("Tool round ended without a typed outcome")
                state.continuation.complete_tool_round(tool_outcome.results)
                if isinstance(tool_outcome, CancelledToolBatch):
                    for event in _cancelled_turn_events(turn):
                        yield event
                    return
            yield TurnCompleted(
                turn=turn,
                outcome="completed",
                finish_reason=response.finish_reason,
            )
            # A boundary-hook failure must not complete this turn a second time.
            turn_started = False
            stop = not had_tool_calls
            if not _is_cancelled(config):
                messages, stop = await at_request_boundary(
                    config,
                    state.continuation,
                    turn=state.turn,
                    tool_iterations=state.tool_iterations,
                    messages=messages,
                    had_tool_calls=had_tool_calls,
                    stop_by_default=stop,
                )
            if stop:
                break
    except Exception as exc:
        overflow_error: ContextOverflowError | None = None
        if isinstance(exc, ContextOverflowError):
            overflow_error = exc
        if overflow_error is not None:
            yield ContextOverflow(
                turn=turn,
                provider=config.provider.name,
                model=config.model or config.provider.default_model,
                context_window=config.context_window,
                message=str(overflow_error),
            )
            if config.defer_context_overflow_errors:
                if overflow_error is not exc:
                    raise overflow_error from exc
                raise
        yield ErrorEvent(message=str(exc))
        if turn_started:
            yield TurnCompleted(turn=turn, outcome="failed", finish_reason="error")
        if overflow_error is not None and overflow_error is not exc:
            raise overflow_error from exc
        raise


__all__ = [
    "AgentLoopEvent",
    "run_agent_loop",
]
