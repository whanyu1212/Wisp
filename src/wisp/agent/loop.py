"""Pure provider and tool-call agent loop."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from wisp.agent.configuration import (
    validate_agent_runtime_limits,
    validate_non_negative_integer,
)
from wisp.agent.context import (
    estimate_context_budget,
    observe_context,
)
from wisp.agent.continuation import (
    ContinuationState,
    at_context_overflow,
    at_request_boundary,
)
from wisp.agent.execution import (
    ContextOverflowHook,
    RequestBoundaryHook,
    ToolExecutor,
)
from wisp.agent.messages import Message
from wisp.agent.provider_turn import (
    CancelledProviderTurn,
    CompletedProviderTurn,
    FailedProviderTurn,
    OverflowProviderTurn,
    ProviderTurn,
    project_usage_and_cost,
)
from wisp.agent.tool_round import CancelledToolRound, ToolRound
from wisp.events import (
    ContextEstimated,
    ContextOverflow,
    ContextPressure,
    ErrorEvent,
    MessageCompleted,
    MessageDelta,
    MessageStarted,
    ProviderRetrying,
    TokenUsage,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolCallSnapshot,
    ToolExecutionEnded,
    ToolExecutionStarted,
    ToolResultReady,
    TurnCompleted,
    TurnStarted,
    UsageCost,
)
from wisp.providers.base import (
    ContextOverflowError,
    Provider,
    ToolSpec,
    is_context_overflow_message,
)

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
type UsageCostEstimator = Callable[[str, str | None, str | None, TokenUsage], UsageCost]


class CancellationToken(Protocol):
    """Cooperative cancellation state observed by the pure loop."""

    def is_cancelled(self) -> bool:
        """Return whether the current run should stop."""
        ...


@dataclass(frozen=True, slots=True)
class AgentLoopConfig:
    """Dependencies and limits for one provider-neutral loop run."""

    provider: Provider
    tool_executor: ToolExecutor
    model: str | None = None
    tools: tuple[ToolSpec, ...] = ()
    max_tool_iterations: int | None = None
    cancellation_token: CancellationToken | None = None
    # Provider-native reasoning-effort tier string (e.g. Anthropic's "high",
    # Google's "MEDIUM", OpenAI's "low") -- not normalized across providers,
    # forwarded to Provider.stream() as-is. None means "use the provider's
    # own default behavior."
    effort: str | None = None
    context_window: int | None = None
    context_reserve_tokens: int = 16_384
    context_pressure_threshold: float = 0.8
    turn_offset: int = 0
    tool_iteration_offset: int = 0
    cost_estimator: UsageCostEstimator | None = None
    defer_context_overflow_errors: bool = False
    prompt_cache_key: str | None = None
    request_boundary_hook: RequestBoundaryHook | None = None
    context_overflow_hook: ContextOverflowHook | None = None

    def __post_init__(self) -> None:
        validate_agent_runtime_limits(
            max_tool_iterations=self.max_tool_iterations,
            context_window=self.context_window,
            context_reserve_tokens=self.context_reserve_tokens,
            context_pressure_threshold=self.context_pressure_threshold,
        )
        validate_non_negative_integer(self.turn_offset, field="turn_offset")
        validate_non_negative_integer(self.tool_iteration_offset, field="tool_iteration_offset")


def _is_cancelled(config: AgentLoopConfig) -> bool:
    token = config.cancellation_token
    return token is not None and token.is_cancelled()


def _cancelled_turn_events(turn: int) -> tuple[ErrorEvent, TurnCompleted]:
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
        self.turn += 1
        return self.turn

    def begin_tool_round(self, maximum: int | None) -> None:
        if maximum is not None and self.tool_iterations >= maximum:
            raise RuntimeError(f"Maximum tool iterations exceeded: {maximum}")
        self.tool_iterations += 1


async def run_agent_loop(
    config: AgentLoopConfig,
    *,
    messages: Sequence[Message],
) -> AsyncGenerator[AgentLoopEvent, None]:
    """Run provider turns and tool cycles without session or frontend dependencies."""

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

            provider_turn = ProviderTurn(
                config,
                messages=messages,
                tool_results=state.continuation.pending_tool_results,
                extra_messages=state.continuation.pending_extra_messages,
                previous_response_id=state.continuation.previous_response_id,
            )
            async for provider_event in provider_turn.events(turn=turn):
                yield provider_event
            outcome = provider_turn.outcome
            if outcome is None:
                raise RuntimeError("Provider turn ended without a typed outcome")
            if isinstance(outcome, CancelledProviderTurn):
                return

            if isinstance(outcome, OverflowProviderTurn):
                # A provider may open a public response lifecycle and then
                # raise instead of yielding a typed terminal failure. Close
                # that lifecycle before recovery starts another turn.
                if outcome.started:
                    yield MessageCompleted(
                        turn=turn,
                        content=outcome.content,
                        finish_reason="error",
                        response_id=outcome.started_response_id,
                    )
                # Preserve the historical raised-overflow path for callers
                # without an explicit same-loop recovery hook. The outer
                # handler owns its public terminal events and re-raises.
                if config.context_overflow_hook is None:
                    raise outcome.error
                yield ContextOverflow(
                    turn=turn,
                    provider=config.provider.name,
                    model=config.model or config.provider.default_model,
                    context_window=config.context_window,
                    message=str(outcome.error),
                )
                messages, retry = await at_context_overflow(
                    config,
                    state.continuation,
                    turn=state.turn,
                    tool_iterations=state.tool_iterations,
                    messages=messages,
                    context_budget=context_budget,
                    had_streamed_delta=outcome.had_streamed_delta,
                    message=str(outcome.error),
                )
                if retry:
                    yield TurnCompleted(turn=turn, outcome="failed", finish_reason="error")
                    turn_started = False
                    continue
                if config.defer_context_overflow_errors:
                    return
                yield ErrorEvent(message=str(outcome.error))
                yield TurnCompleted(turn=turn, outcome="failed", finish_reason="error")
                return
            if isinstance(outcome, FailedProviderTurn):
                failure = outcome.failed.response
                failure_kind = (
                    "context_overflow"
                    if failure.failure_kind == "context_overflow"
                    or is_context_overflow_message(failure.message)
                    else failure.failure_kind
                )
                if outcome.started:
                    yield MessageCompleted(
                        turn=turn,
                        content=outcome.failed.content,
                        finish_reason="error",
                        response_id=outcome.failed.response_id,
                    )
                if failure_kind == "context_overflow":
                    yield ContextOverflow(
                        turn=turn,
                        provider=config.provider.name,
                        model=config.model or config.provider.default_model,
                        context_window=config.context_window,
                        message=failure.message,
                    )
                    messages, retry = await at_context_overflow(
                        config,
                        state.continuation,
                        turn=state.turn,
                        tool_iterations=state.tool_iterations,
                        messages=messages,
                        context_budget=context_budget,
                        had_streamed_delta=outcome.had_streamed_delta,
                        message=failure.message,
                    )
                    if retry:
                        yield TurnCompleted(turn=turn, outcome="failed", finish_reason="error")
                        turn_started = False
                        continue
                    if config.defer_context_overflow_errors:
                        return
                yield ErrorEvent(message=failure.message)
                yield TurnCompleted(
                    turn=turn,
                    outcome="cancelled" if failure_kind == "aborted" else "failed",
                    finish_reason="cancelled" if failure_kind == "aborted" else "error",
                )
                return
            if not isinstance(outcome, CompletedProviderTurn):
                raise RuntimeError("Provider turn ended without a completed response")
            completed = outcome.completed
            response = completed.response
            completed_content = completed.content
            tool_calls = response.tool_calls
            response_id = completed.response_id
            usage, cost = project_usage_and_cost(config, completed)
            tool_call_snapshots = tuple(
                ToolCallSnapshot(
                    call_id=tool_call.call_id,
                    name=tool_call.name,
                    arguments=dict(tool_call.arguments),
                    provider_call_id=tool_call.provider_call_id,
                    parse_error=tool_call.parse_error,
                )
                for tool_call in tool_calls
            )
            # Completion events cross a public yield boundary. Deep-copy the
            # snapshots before yielding so consumer mutation cannot alter provider
            # continuation state.
            continuation_tool_calls = tuple(
                snapshot.model_copy(deep=True) for snapshot in tool_call_snapshots
            )
            context_observation = (
                observe_context(
                    request_messages,
                    config.tools,
                    provider=config.provider.name,
                    model=selected_model,
                    input_tokens=(
                        response.usage.context_input_tokens
                        if response.usage is not None
                        and response.usage.context_input_tokens is not None
                        else usage.input_tokens
                    ),
                )
                if usage is not None
                else None
            )
            yield MessageCompleted(
                turn=turn,
                content=completed_content,
                finish_reason=response.finish_reason,
                response_id=response_id,
                usage=usage,
                cost=cost,
                context_observation=context_observation,
                tool_calls=tool_call_snapshots,
            )
            continuation_message = Message(
                role="assistant",
                content=completed_content,
                response_id=response_id,
                finish_reason=response.finish_reason,
                usage=usage,
                cost=cost,
                context_observation=context_observation,
                tool_calls=continuation_tool_calls,
            )
            state.continuation.record_response(
                continuation_message, response_id=completed.response_id
            )
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

            if not tool_calls:
                yield TurnCompleted(
                    turn=turn,
                    outcome="completed",
                    finish_reason=response.finish_reason,
                )
                # This turn has already yielded its one terminal event -- a
                # boundary-hook failure past this point is not a failure *of*
                # `turn` and must not produce a second, contradictory
                # TurnCompleted for it in the except block below.
                turn_started = False
                if not _is_cancelled(config):
                    messages, stop = await at_request_boundary(
                        config,
                        state.continuation,
                        turn=state.turn,
                        tool_iterations=state.tool_iterations,
                        messages=messages,
                        had_tool_calls=False,
                        stop_by_default=True,
                    )
                    if not stop:
                        continue
                break
            if _is_cancelled(config):
                for event in _cancelled_turn_events(turn):
                    yield event
                break
            state.begin_tool_round(config.max_tool_iterations)
            tool_round = ToolRound(
                tool_executor=config.tool_executor,
                tool_calls=tool_calls,
                truncated=response.finish_reason == "length",
                is_cancelled=lambda: _is_cancelled(config),
                on_result=state.continuation.record_tool_result,
            )
            async for tool_event in tool_round.events():
                yield tool_event
            tool_outcome = tool_round.outcome
            if tool_outcome is None:
                raise RuntimeError("Tool round ended without a typed outcome")
            state.continuation.complete_tool_round(tool_outcome.results)
            if isinstance(tool_outcome, CancelledToolRound):
                for event in _cancelled_turn_events(turn):
                    yield event
                return
            yield TurnCompleted(
                turn=turn,
                outcome="completed",
                finish_reason=response.finish_reason,
            )
            # See the no-tool-calls boundary above: this turn's one terminal
            # event has already been yielded.
            turn_started = False
            if not _is_cancelled(config):
                messages, stop = await at_request_boundary(
                    config,
                    state.continuation,
                    turn=state.turn,
                    tool_iterations=state.tool_iterations,
                    messages=messages,
                    had_tool_calls=True,
                    stop_by_default=False,
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
    "AgentLoopConfig",
    "AgentLoopEvent",
    "CancellationToken",
    "run_agent_loop",
]
