"""Pure provider and tool-call agent loop."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, cast

import wisp.providers.events as provider_events
from wisp.agent.configuration import (
    validate_agent_runtime_limits,
    validate_non_negative_integer,
)
from wisp.agent.context import build_context_budget, estimate_context
from wisp.agent.execution import (
    ToolExecutionEvent,
    ToolExecutionProtocolError,
    ToolExecutor,
)
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
    PromptCacheKeyProvider,
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


def _provider_stream(
    config: AgentLoopConfig,
    *,
    messages: Sequence[Message],
    tool_results: Sequence[ToolCallResult],
    previous_response_id: str | None,
) -> AsyncIterator[provider_events.ProviderEvent]:
    """Call one provider without imposing optional keywords on legacy adapters."""

    provider = config.provider
    if (
        config.prompt_cache_key is not None
        and getattr(provider, "supports_prompt_cache_key", False) is True
    ):
        cache_provider = cast(PromptCacheKeyProvider, provider)
        if config.effort is not None:
            return cache_provider.stream(
                messages,
                model=config.model,
                tools=config.tools,
                tool_results=tool_results,
                previous_response_id=previous_response_id,
                effort=config.effort,
                prompt_cache_key=config.prompt_cache_key,
            )
        return cache_provider.stream(
            messages,
            model=config.model,
            tools=config.tools,
            tool_results=tool_results,
            previous_response_id=previous_response_id,
            prompt_cache_key=config.prompt_cache_key,
        )
    if config.effort is not None:
        return provider.stream(
            messages,
            model=config.model,
            tools=config.tools,
            tool_results=tool_results,
            previous_response_id=previous_response_id,
            effort=config.effort,
        )
    return provider.stream(
        messages,
        model=config.model,
        tools=config.tools,
        tool_results=tool_results,
        previous_response_id=previous_response_id,
    )


def _cancelled_turn_events(turn: int) -> tuple[ErrorEvent, TurnCompleted]:
    return (
        ErrorEvent(message="Agent run cancelled"),
        TurnCompleted(turn=turn, outcome="cancelled", finish_reason="cancelled"),
    )


def _unavailable_cost(
    provider: str,
    requested_model: str | None,
    response_model: str | None,
    *,
    reason: Literal["pricing_unavailable", "usage_incomplete", "estimation_failed"],
) -> UsageCost:
    """Keep optional accounting failures from discarding a completed provider response."""

    return UsageCost(
        provider=provider,
        requested_model=requested_model,
        model=response_model or requested_model,
        unavailable_reason=reason,
    )


def _require_provider_response_started(started: bool) -> None:
    if not started:
        raise ProviderProtocolError("Provider emitted response data before response_started")


def _resolve_provider_response_id(
    *,
    started_response_id: str | None,
    terminal_response_id: str | None,
    tool_calls: Sequence[ToolCall],
) -> str | None:
    """Resolve one consistent response id from a provider lifecycle."""

    candidates = [
        ("response_started", started_response_id),
        ("terminal response", terminal_response_id),
        *((f"tool call {tool_call.call_id}", tool_call.response_id) for tool_call in tool_calls),
    ]
    supplied = [
        (source, response_id) for source, response_id in candidates if response_id is not None
    ]
    if not supplied:
        return None

    resolved_source, resolved_id = supplied[0]
    for source, response_id in supplied[1:]:
        if response_id != resolved_id:
            raise ProviderProtocolError(
                "Provider emitted conflicting response ids: "
                f"{resolved_source}={resolved_id!r}, {source}={response_id!r}"
            )
    return resolved_id


@dataclass(frozen=True, slots=True)
class _CompletedProviderResponse:
    """Validated provider response assembled from one streamed lifecycle."""

    response: ProviderResponseCompleted
    content: str
    thinking: str
    response_id: str | None
    response_model: str | None


@dataclass(slots=True)
class _ProviderResponseLifecycle:
    """Own and validate the state transitions for one provider response."""

    started: bool = False
    started_response_id: str | None = None
    response_model: str | None = None
    terminal: ProviderResponseCompleted | ProviderResponseFailed | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    thinking: list[str] = field(default_factory=list)

    def require_open(self) -> None:
        if self.terminal is not None:
            raise ProviderProtocolError("Provider emitted an event after its terminal response")

    def start(self, event: ProviderResponseStarted) -> None:
        self.require_open()
        if self.started:
            raise ProviderProtocolError("Provider emitted response_started more than once")
        self.started = True
        self.started_response_id = event.response_id
        self.response_model = event.model

    def retry(self) -> None:
        self.require_open()
        if self.started:
            raise ProviderProtocolError("Provider emitted retry progress after response_started")

    def add_text(self, delta: str) -> None:
        self.require_open()
        _require_provider_response_started(self.started)
        self.text.append(delta)

    def add_thinking(self, delta: str) -> None:
        self.require_open()
        _require_provider_response_started(self.started)
        self.thinking.append(delta)

    def add_tool_call(self, tool_call: ToolCall) -> None:
        self.require_open()
        _require_provider_response_started(self.started)
        self.tool_calls.append(tool_call)

    def complete(self, event: ProviderResponseCompleted | ProviderResponseFailed) -> None:
        self.require_open()
        _require_provider_response_started(self.started)
        self.terminal = event

    def finish(self) -> _CompletedProviderResponse:
        if not self.started:
            raise ProviderProtocolError("Provider stream ended before response_started")
        if self.terminal is None:
            raise ProviderProtocolError("Provider stream ended without a terminal response")
        if isinstance(self.terminal, ProviderResponseFailed):
            _resolve_provider_response_id(
                started_response_id=self.started_response_id,
                terminal_response_id=self.terminal.response_id,
                tool_calls=self.tool_calls,
            )
            if is_context_overflow_message(self.terminal.message):
                raise ContextOverflowError(self.terminal.message)
            raise ProviderError(self.terminal.message)
        if tuple(self.tool_calls) != self.terminal.tool_calls:
            raise ProviderProtocolError(
                "Provider terminal tool calls do not match streamed tool calls"
            )
        response_id = _resolve_provider_response_id(
            started_response_id=self.started_response_id,
            terminal_response_id=self.terminal.response_id,
            tool_calls=self.terminal.tool_calls,
        )
        return _CompletedProviderResponse(
            response=self.terminal,
            content=self.terminal.content or "".join(self.text),
            thinking="".join(self.thinking),
            response_id=response_id,
            response_model=self.response_model,
        )


@dataclass(slots=True)
class _AgentLoopState:
    """Mutable continuation state shared across provider/tool turns."""

    turn: int
    tool_iterations: int
    pending_tool_results: tuple[ToolCallResult, ...] = ()
    previous_response_id: str | None = None
    continuation_messages: list[Message] = field(default_factory=list)

    def begin_turn(self) -> int:
        self.turn += 1
        return self.turn

    def record_response(self, completed: _CompletedProviderResponse, message: Message) -> None:
        self.previous_response_id = completed.response_id
        self.continuation_messages.append(message)

    def begin_tool_round(self, maximum: int | None) -> None:
        if maximum is not None and self.tool_iterations >= maximum:
            raise RuntimeError(f"Maximum tool iterations exceeded: {maximum}")
        self.tool_iterations += 1

    def complete_tool_round(self, results: Sequence[ToolCallResult]) -> None:
        self.pending_tool_results = tuple(results)


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


def _json_payloads_match(left: object, right: object) -> bool:
    """Compare JSON payloads canonically without conflating booleans and numbers."""

    try:
        return json.dumps(
            left,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ) == json.dumps(
            right,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        return False


@dataclass(slots=True)
class _ToolExecutionLifecycle:
    """Validate one executor stream before its result reaches the provider."""

    tool_call: ToolCall
    approval_requested: bool = False
    approval_resolved: bool = False
    approved: bool | None = None
    terminal: ToolExecutionEnded | None = None

    def accept(self, event: object) -> ToolExecutionEvent:
        if not isinstance(event, ToolApprovalRequested | ToolApprovalResolved | ToolExecutionEnded):
            raise ToolExecutionProtocolError(
                "Tool executor emitted an unsupported event type for "
                f"{self.tool_call.call_id}: {type(event).__name__}"
            )
        if self.terminal is not None:
            raise ToolExecutionProtocolError(
                f"Tool executor emitted an event after the result for {self.tool_call.call_id}"
            )
        if event.call_id != self.tool_call.call_id or event.name != self.tool_call.name:
            raise ToolExecutionProtocolError(
                "Tool executor event does not match the requested call: "
                f"expected {self.tool_call.name}/{self.tool_call.call_id}, "
                f"got {event.name}/{event.call_id}"
            )

        if isinstance(event, ToolApprovalRequested):
            if self.approval_requested:
                raise ToolExecutionProtocolError(
                    f"Tool executor requested approval more than once for {self.tool_call.call_id}"
                )
            if not _json_payloads_match(event.arguments, self.tool_call.arguments):
                raise ToolExecutionProtocolError(
                    "Tool executor approval arguments do not match the requested call "
                    f"{self.tool_call.call_id}"
                )
            self.approval_requested = True
        elif isinstance(event, ToolApprovalResolved):
            if not self.approval_requested:
                raise ToolExecutionProtocolError(
                    "Tool executor resolved approval before requesting it for "
                    f"{self.tool_call.call_id}"
                )
            if self.approval_resolved:
                raise ToolExecutionProtocolError(
                    f"Tool executor resolved approval more than once for {self.tool_call.call_id}"
                )
            self.approval_resolved = True
            self.approved = event.approved
        else:
            if self.approval_requested and not self.approval_resolved:
                raise ToolExecutionProtocolError(
                    f"Tool executor ended with an unresolved approval for {self.tool_call.call_id}"
                )
            if self.approved is False and not event.is_error:
                raise ToolExecutionProtocolError(
                    "Tool executor reported success after approval was denied for "
                    f"{self.tool_call.call_id}"
                )
            self.terminal = event
        return event

    def finish(self) -> ToolExecutionEnded:
        if self.approval_requested and not self.approval_resolved:
            raise ToolExecutionProtocolError(
                f"Tool executor ended with an unresolved approval for {self.tool_call.call_id}"
            )
        if self.terminal is None:
            raise ToolExecutionProtocolError(
                f"Tool executor ended without a result for {self.tool_call.call_id}"
            )
        return self.terminal


async def _execute_tool_call(
    config: AgentLoopConfig,
    tool_call: ToolCall,
) -> AsyncIterator[ToolExecutionEvent | ToolResultReady]:
    lifecycle = _ToolExecutionLifecycle(tool_call)
    async for raw_event in config.tool_executor.execute(tool_call):
        event = lifecycle.accept(raw_event)
        if not isinstance(event, ToolExecutionEnded):
            yield event

    terminal = lifecycle.finish()
    yield terminal
    yield ToolResultReady.from_execution_ended(terminal)


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

    try:
        while True:
            if _is_cancelled(config):
                yield ErrorEvent(message="Agent run cancelled")
                break
            turn = state.begin_turn()
            yield TurnStarted(turn=turn)
            if _is_cancelled(config):
                for event in _cancelled_turn_events(turn):
                    yield event
                break
            lifecycle = _ProviderResponseLifecycle()

            estimate = estimate_context((*messages, *state.continuation_messages), config.tools)
            yield ContextEstimated(
                turn=turn,
                provider=config.provider.name,
                model=config.model or config.provider.default_model,
                budget=build_context_budget(
                    estimate,
                    context_window=config.context_window,
                    reserve_tokens=config.context_reserve_tokens,
                ),
            )

            try:
                provider_stream = _provider_stream(
                    config,
                    messages=messages,
                    tool_results=state.pending_tool_results,
                    previous_response_id=state.previous_response_id,
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
                lifecycle.require_open()
                if isinstance(provider_event, ProviderResponseStarted):
                    lifecycle.start(provider_event)
                    yield MessageStarted(turn=turn)
                elif isinstance(provider_event, provider_events.ProviderRetrying):
                    lifecycle.retry()
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
                    lifecycle.add_text(provider_event.delta)
                    yield MessageDelta(
                        turn=turn,
                        delta=provider_event.delta,
                        content_index=provider_event.content_index,
                    )
                elif isinstance(provider_event, ProviderThinkingDelta):
                    lifecycle.add_thinking(provider_event.delta)
                    yield MessageDelta(
                        turn=turn,
                        delta=provider_event.delta,
                        content_index=provider_event.content_index,
                        content_kind="thinking",
                    )
                elif isinstance(provider_event, ProviderToolCallCompleted):
                    lifecycle.add_tool_call(provider_event.tool_call)
                elif isinstance(provider_event, ProviderResponseCompleted | ProviderResponseFailed):
                    lifecycle.complete(provider_event)
                else:
                    raise ProviderProtocolError(
                        f"Provider emitted unsupported event type: {type(provider_event).__name__}"
                    )

            if _is_cancelled(config):
                for event in _cancelled_turn_events(turn):
                    yield event
                break
            completed = lifecycle.finish()
            response = completed.response
            completed_content = completed.content
            tool_calls = response.tool_calls
            response_id = completed.response_id
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
            if usage is None:
                cost = _unavailable_cost(
                    config.provider.name,
                    config.model,
                    completed.response_model,
                    reason="usage_incomplete",
                )
            elif config.cost_estimator is None:
                cost = _unavailable_cost(
                    config.provider.name,
                    config.model,
                    completed.response_model,
                    reason="pricing_unavailable",
                )
            else:
                try:
                    cost = config.cost_estimator(
                        config.provider.name,
                        config.model,
                        completed.response_model,
                        usage,
                    )
                except Exception:
                    cost = _unavailable_cost(
                        config.provider.name,
                        config.model,
                        completed.response_model,
                        reason="estimation_failed",
                    )
            tool_call_snapshots = tuple(
                ToolCallSnapshot(
                    call_id=tool_call.call_id,
                    name=tool_call.name,
                    arguments=dict(tool_call.arguments),
                    parse_error=tool_call.parse_error,
                )
                for tool_call in tool_calls
            )
            yield MessageCompleted(
                turn=turn,
                content=completed_content,
                finish_reason=response.finish_reason,
                response_id=response_id,
                usage=usage,
                cost=cost,
                tool_calls=tool_call_snapshots,
            )
            continuation_message = Message(
                role="assistant",
                content=completed_content + completed.thinking,
                response_id=response_id,
                finish_reason=response.finish_reason,
                usage=usage,
                cost=cost,
                tool_calls=tool_call_snapshots,
            )
            state.record_response(completed, continuation_message)
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
            state.begin_tool_round(config.max_tool_iterations)
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
                state.continuation_messages.append(
                    Message(
                        role="tool",
                        content=result_event.output,
                        tool_call_id=result_event.call_id,
                        tool_name=result_event.name,
                        is_error=result_event.is_error,
                    )
                )
            state.complete_tool_round(tool_results)
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
            if config.defer_context_overflow_errors:
                if overflow_error is not exc:
                    raise overflow_error from exc
                raise
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
