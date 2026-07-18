"""Pure provider and tool-call agent loop."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol

import wisp.providers.events as provider_events
from wisp.agent.execution import (
    ToolExecutionEvent,
    ToolExecutionProtocolError,
    ToolExecutor,
)
from wisp.agent.messages import Message
from wisp.events import (
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
)
from wisp.providers.base import (
    ContextOverflowError,
    Provider,
    ProviderError,
    ProviderProtocolError,
    ToolCallResult,
    ToolSpec,
    is_context_overflow_message,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderThinkingDelta,
    ProviderToolCallCompleted,
    ToolCall,
)

type AgentLoopEvent = (
    TurnStarted
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
    context_pressure_threshold: float = 0.8

    def __post_init__(self) -> None:
        if self.context_window is not None and self.context_window <= 0:
            raise ValueError("context_window must be positive")
        if not 0 < self.context_pressure_threshold <= 1:
            raise ValueError("context_pressure_threshold must be greater than 0 and at most 1")


def _is_cancelled(config: AgentLoopConfig) -> bool:
    token = config.cancellation_token
    return token is not None and token.is_cancelled()


def _cancelled_turn_events(turn: int) -> tuple[ErrorEvent, TurnCompleted]:
    return (
        ErrorEvent(message="Agent run cancelled"),
        TurnCompleted(turn=turn, outcome="cancelled", finish_reason="cancelled"),
    )


def _require_provider_response_started(started: bool) -> None:
    if not started:
        raise ProviderProtocolError("Provider emitted response data before response_started")


async def _provider_events(
    stream: AsyncIterator[provider_events.ProviderEvent],
) -> AsyncIterator[provider_events.ProviderEvent]:
    """Normalize context overflows raised while advancing a provider stream."""

    iterator = aiter(stream)
    while True:
        try:
            event = await anext(iterator)
        except StopAsyncIteration:
            return
        except Exception as exc:
            if is_context_overflow_message(str(exc)):
                raise ContextOverflowError(str(exc)) from exc
            raise
        yield event


def _validate_execution_event(event: ToolExecutionEvent, tool_call: ToolCall) -> None:
    if event.call_id != tool_call.call_id or event.name != tool_call.name:
        raise ToolExecutionProtocolError(
            "Tool executor event does not match the requested call: "
            f"expected {tool_call.name}/{tool_call.call_id}, "
            f"got {event.name}/{event.call_id}"
        )


async def _execute_tool_call(
    config: AgentLoopConfig,
    tool_call: ToolCall,
) -> AsyncIterator[ToolExecutionEvent | ToolResultReady]:
    terminal: ToolExecutionEnded | None = None
    async for event in config.tool_executor.execute(tool_call):
        if terminal is not None:
            raise ToolExecutionProtocolError(
                f"Tool executor emitted an event after the result for {tool_call.call_id}"
            )
        _validate_execution_event(event, tool_call)
        if isinstance(event, ToolExecutionEnded):
            terminal = event
        else:
            yield event

    if terminal is None:
        raise ToolExecutionProtocolError(
            f"Tool executor ended without a result for {tool_call.call_id}"
        )
    yield terminal
    yield ToolResultReady(
        call_id=terminal.call_id,
        name=terminal.name,
        output=terminal.output,
        is_error=terminal.is_error,
        exit_code=terminal.exit_code,
        before_text=terminal.before_text,
        created=terminal.created,
        summary=terminal.summary,
        truncated=terminal.truncated,
    )


async def run_agent_loop(
    config: AgentLoopConfig,
    *,
    messages: Sequence[Message],
) -> AsyncGenerator[AgentLoopEvent, None]:
    """Run provider turns and tool cycles without session or frontend dependencies."""

    pending_tool_results: tuple[ToolCallResult, ...] = ()
    previous_response_id: str | None = None
    tool_iterations = 0
    turn = 0

    try:
        while True:
            if _is_cancelled(config):
                yield ErrorEvent(message="Agent run cancelled")
                break
            turn += 1
            yield TurnStarted(turn=turn)
            if _is_cancelled(config):
                for event in _cancelled_turn_events(turn):
                    yield event
                break
            response_started = False
            terminal_response: ProviderResponseCompleted | ProviderResponseFailed | None = None
            streamed_tool_calls: list[ToolCall] = []

            # `effort` is only passed when actually set, not unconditionally
            # as None: it is a newer, optional Provider.stream() keyword, and
            # Provider is a structural Protocol with no runtime enforcement
            # -- a third-party provider implemented against the pre-effort
            # signature would otherwise get a TypeError on every turn instead
            # of keeping its unchanged default behavior.
            try:
                if config.effort is not None:
                    provider_stream = config.provider.stream(
                        messages,
                        model=config.model,
                        tools=config.tools,
                        tool_results=pending_tool_results,
                        previous_response_id=previous_response_id,
                        effort=config.effort,
                    )
                else:
                    provider_stream = config.provider.stream(
                        messages,
                        model=config.model,
                        tools=config.tools,
                        tool_results=pending_tool_results,
                        previous_response_id=previous_response_id,
                    )
            except Exception as exc:
                if is_context_overflow_message(str(exc)):
                    raise ContextOverflowError(str(exc)) from exc
                raise
            async for provider_event in _provider_events(provider_stream):
                if _is_cancelled(config):
                    for event in _cancelled_turn_events(turn):
                        yield event
                    return
                if terminal_response is not None:
                    raise ProviderProtocolError(
                        "Provider emitted an event after its terminal response"
                    )
                if isinstance(provider_event, ProviderResponseStarted):
                    if response_started:
                        raise ProviderProtocolError(
                            "Provider emitted response_started more than once"
                        )
                    response_started = True
                    yield MessageStarted(turn=turn)
                elif isinstance(provider_event, provider_events.ProviderRetrying):
                    if response_started:
                        raise ProviderProtocolError(
                            "Provider emitted retry progress after response_started"
                        )
                    yield ProviderRetrying(
                        turn=turn,
                        provider=config.provider.name,
                        attempt=provider_event.attempt,
                        max_attempts=provider_event.max_attempts,
                        delay_seconds=provider_event.delay_seconds,
                        reason=provider_event.reason,
                        status_code=provider_event.status_code,
                    )
                elif isinstance(provider_event, ProviderTextDelta):
                    _require_provider_response_started(response_started)
                    yield MessageDelta(
                        turn=turn,
                        delta=provider_event.delta,
                        content_index=provider_event.content_index,
                    )
                elif isinstance(provider_event, ProviderThinkingDelta):
                    _require_provider_response_started(response_started)
                    yield MessageDelta(
                        turn=turn,
                        delta=provider_event.delta,
                        content_index=provider_event.content_index,
                        content_kind="thinking",
                    )
                elif isinstance(provider_event, ProviderToolCallCompleted):
                    _require_provider_response_started(response_started)
                    streamed_tool_calls.append(provider_event.tool_call)
                elif isinstance(provider_event, ProviderResponseCompleted | ProviderResponseFailed):
                    _require_provider_response_started(response_started)
                    terminal_response = provider_event
                else:
                    raise ProviderProtocolError(
                        f"Provider emitted unsupported event type: {type(provider_event).__name__}"
                    )

            if _is_cancelled(config):
                for event in _cancelled_turn_events(turn):
                    yield event
                break
            if not response_started:
                raise ProviderProtocolError("Provider stream ended before response_started")
            if terminal_response is None:
                raise ProviderProtocolError("Provider stream ended without a terminal response")
            if isinstance(terminal_response, ProviderResponseFailed):
                if is_context_overflow_message(terminal_response.message):
                    raise ContextOverflowError(terminal_response.message)
                raise ProviderError(terminal_response.message)
            if tuple(streamed_tool_calls) != terminal_response.tool_calls:
                raise ProviderProtocolError(
                    "Provider terminal tool calls do not match streamed tool calls"
                )

            response = terminal_response
            tool_calls = response.tool_calls
            response_id = response.response_id
            if response_id is None:
                response_id = next(
                    (
                        tool_call.response_id
                        for tool_call in reversed(tool_calls)
                        if tool_call.response_id is not None
                    ),
                    None,
                )
            previous_response_id = response_id
            usage = (
                TokenUsage(
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    total_tokens=response.usage.total_tokens,
                    cache_read_input_tokens=response.usage.cache_read_input_tokens,
                    cache_write_input_tokens=response.usage.cache_write_input_tokens,
                    reasoning_output_tokens=response.usage.reasoning_output_tokens,
                )
                if response.usage is not None
                else None
            )
            yield MessageCompleted(
                turn=turn,
                content=response.content,
                finish_reason=response.finish_reason,
                response_id=response_id,
                usage=usage,
                tool_calls=tuple(
                    ToolCallSnapshot(
                        call_id=tool_call.call_id,
                        name=tool_call.name,
                        arguments=dict(tool_call.arguments),
                        parse_error=tool_call.parse_error,
                    )
                    for tool_call in tool_calls
                ),
            )
            if usage is not None and config.context_window is not None:
                pressure_ratio = usage.total_tokens / config.context_window
                if pressure_ratio >= config.context_pressure_threshold:
                    yield ContextPressure(
                        turn=turn,
                        provider=config.provider.name,
                        model=config.model or config.provider.default_model,
                        context_window=config.context_window,
                        observed_tokens=usage.total_tokens,
                        remaining_tokens=max(0, config.context_window - usage.total_tokens),
                        pressure_ratio=pressure_ratio,
                    )

            if not tool_calls:
                yield TurnCompleted(
                    turn=turn,
                    outcome="completed",
                    finish_reason=response.finish_reason,
                )
                break
            if _is_cancelled(config):
                for event in _cancelled_turn_events(turn):
                    yield event
                break
            if (
                config.max_tool_iterations is not None
                and tool_iterations >= config.max_tool_iterations
            ):
                raise RuntimeError(
                    f"Maximum tool iterations exceeded: {config.max_tool_iterations}"
                )

            tool_iterations += 1
            tool_results: list[ToolCallResult] = []
            for tool_call in tool_calls:
                if _is_cancelled(config):
                    for event in _cancelled_turn_events(turn):
                        yield event
                    return
                arguments = dict(tool_call.arguments)
                yield ToolCallRequested(
                    call_id=tool_call.call_id,
                    name=tool_call.name,
                    arguments=arguments,
                )
                if _is_cancelled(config):
                    for event in _cancelled_turn_events(turn):
                        yield event
                    return
                yield ToolExecutionStarted(
                    call_id=tool_call.call_id,
                    name=tool_call.name,
                    arguments=arguments,
                )
                if _is_cancelled(config):
                    for event in _cancelled_turn_events(turn):
                        yield event
                    return
                result_event: ToolResultReady | None = None
                async for execution_event in _execute_tool_call(config, tool_call):
                    yield execution_event
                    if isinstance(execution_event, ToolResultReady):
                        result_event = execution_event
                    if _is_cancelled(config) and not isinstance(
                        execution_event, ToolExecutionEnded
                    ):
                        for event in _cancelled_turn_events(turn):
                            yield event
                        return
                if result_event is None:
                    raise ToolExecutionProtocolError(
                        f"Tool executor produced no provider result for {tool_call.call_id}"
                    )
                tool_results.append(
                    ToolCallResult(
                        call_id=result_event.call_id,
                        output=result_event.output,
                        is_error=result_event.is_error,
                    )
                )
            pending_tool_results = tuple(tool_results)
            yield TurnCompleted(
                turn=turn,
                outcome="completed",
                finish_reason=response.finish_reason,
            )
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
        yield ErrorEvent(message=str(exc))
        if turn > 0:
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
