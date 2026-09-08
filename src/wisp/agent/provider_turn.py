"""One provider-turn stream, lifecycle validation, and cost projection."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast

import wisp.providers.events as provider_events
from wisp.agent.continuation import provider_supports_continuation_messages
from wisp.agent.messages import Message
from wisp.events import (
    ErrorEvent,
    MessageDelta,
    MessageStarted,
    ProviderRetrying,
    TokenUsage,
    TurnCompleted,
    UsageCost,
)
from wisp.providers.base import (
    ContextOverflowError,
    ContinuationMessageProvider,
    PromptCacheContinuationMessageProvider,
    PromptCacheKeyProvider,
    Provider,
    ProviderProtocolError,
    ToolCallResult,
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

if TYPE_CHECKING:
    from wisp.agent.loop import AgentLoopConfig

type ProviderTurnEvent = (
    MessageStarted | ProviderRetrying | MessageDelta | ErrorEvent | TurnCompleted
)


def provider_supports_prompt_cache_key(provider: Provider) -> bool:
    return getattr(provider, "supports_prompt_cache_key", False) is True


def open_provider_stream(
    config: AgentLoopConfig,
    *,
    messages: Sequence[Message],
    tool_results: Sequence[ToolCallResult],
    extra_messages: Sequence[Message],
    previous_response_id: str | None,
) -> AsyncIterator[provider_events.ProviderEvent]:
    """Call one provider without imposing optional keywords on legacy adapters."""

    provider = config.provider
    supports_continuation_messages = provider_supports_continuation_messages(provider)
    supports_prompt_cache_key = provider_supports_prompt_cache_key(provider)
    use_prompt_cache_key = config.prompt_cache_key is not None and supports_prompt_cache_key

    if extra_messages and supports_continuation_messages and use_prompt_cache_key:
        combined_provider = cast(PromptCacheContinuationMessageProvider, provider)
        if config.effort is not None:
            return combined_provider.stream(
                messages,
                model=config.model,
                tools=config.tools,
                tool_results=tool_results,
                extra_messages=extra_messages,
                previous_response_id=previous_response_id,
                effort=config.effort,
                prompt_cache_key=config.prompt_cache_key,
            )
        return combined_provider.stream(
            messages,
            model=config.model,
            tools=config.tools,
            tool_results=tool_results,
            extra_messages=extra_messages,
            previous_response_id=previous_response_id,
            prompt_cache_key=config.prompt_cache_key,
        )

    if extra_messages and supports_continuation_messages:
        continuation_provider = cast(ContinuationMessageProvider, provider)
        if config.effort is not None:
            return continuation_provider.stream(
                messages,
                model=config.model,
                tools=config.tools,
                tool_results=tool_results,
                extra_messages=extra_messages,
                previous_response_id=previous_response_id,
                effort=config.effort,
            )
        return continuation_provider.stream(
            messages,
            model=config.model,
            tools=config.tools,
            tool_results=tool_results,
            extra_messages=extra_messages,
            previous_response_id=previous_response_id,
        )

    if use_prompt_cache_key:
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


async def iter_provider_events(
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


def unavailable_cost(
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


def require_provider_response_started(started: bool) -> None:
    if not started:
        raise ProviderProtocolError("Provider emitted response data before response_started")


def resolve_provider_response_id(
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
class CompletedProviderResponse:
    """Validated successful provider response assembled from one stream."""

    response: ProviderResponseCompleted
    content: str
    response_id: str | None
    response_model: str | None


@dataclass(frozen=True, slots=True)
class FailedProviderResponse:
    """Validated terminal provider failure assembled from one stream."""

    response: ProviderResponseFailed
    content: str
    response_id: str | None


@dataclass(slots=True)
class ProviderResponseLifecycle:
    """Own and validate the state transitions for one provider response."""

    started: bool = False
    started_response_id: str | None = None
    response_model: str | None = None
    terminal: ProviderResponseCompleted | ProviderResponseFailed | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    text: list[str] = field(default_factory=list)

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
        require_provider_response_started(self.started)
        self.text.append(delta)

    def add_thinking(self, delta: str) -> None:
        self.require_open()
        require_provider_response_started(self.started)
        del delta

    def add_tool_call(self, tool_call: ToolCall) -> None:
        self.require_open()
        require_provider_response_started(self.started)
        self.tool_calls.append(tool_call)

    def complete(self, event: ProviderResponseCompleted | ProviderResponseFailed) -> None:
        self.require_open()
        if isinstance(event, ProviderResponseCompleted):
            require_provider_response_started(self.started)
        self.terminal = event

    def finish(self) -> CompletedProviderResponse | FailedProviderResponse:
        if self.terminal is None:
            if not self.started:
                raise ProviderProtocolError("Provider stream ended before response_started")
            raise ProviderProtocolError("Provider stream ended without a terminal response")
        if isinstance(self.terminal, ProviderResponseFailed):
            response_id = resolve_provider_response_id(
                started_response_id=self.started_response_id,
                terminal_response_id=self.terminal.response_id,
                tool_calls=self.tool_calls,
            )
            return FailedProviderResponse(
                response=self.terminal,
                content=self.terminal.partial_content or "".join(self.text),
                response_id=response_id,
            )
        if not self.started:
            raise ProviderProtocolError("Provider stream ended before response_started")
        if tuple(self.tool_calls) != self.terminal.tool_calls:
            raise ProviderProtocolError(
                "Provider terminal tool calls do not match streamed tool calls"
            )
        has_tool_calls = bool(self.terminal.tool_calls)
        if self.terminal.finish_reason == "tool_calls" and not has_tool_calls:
            raise ProviderProtocolError(
                "Provider finish reason 'tool_calls' requires at least one tool call"
            )
        if self.terminal.finish_reason == "stop" and has_tool_calls:
            raise ProviderProtocolError("Provider finish reason 'stop' cannot include tool calls")
        response_id = resolve_provider_response_id(
            started_response_id=self.started_response_id,
            terminal_response_id=self.terminal.response_id,
            tool_calls=self.terminal.tool_calls,
        )
        return CompletedProviderResponse(
            response=self.terminal,
            content=self.terminal.content or "".join(self.text),
            response_id=response_id,
            response_model=self.response_model,
        )


def project_usage_and_cost(
    config: AgentLoopConfig,
    completed: CompletedProviderResponse,
) -> tuple[TokenUsage | None, UsageCost]:
    """Project usage and cost without letting accounting failures discard the response."""

    response = completed.response
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
        cost = unavailable_cost(
            config.provider.name,
            config.model,
            completed.response_model,
            reason="usage_incomplete",
        )
    elif config.cost_estimator is None:
        cost = unavailable_cost(
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
            cost = unavailable_cost(
                config.provider.name,
                config.model,
                completed.response_model,
                reason="estimation_failed",
            )
    return usage, cost


@dataclass(frozen=True, slots=True)
class CompletedProviderTurn:
    completed: CompletedProviderResponse
    had_streamed_delta: bool


@dataclass(frozen=True, slots=True)
class FailedProviderTurn:
    failed: FailedProviderResponse
    started: bool
    had_streamed_delta: bool


@dataclass(frozen=True, slots=True)
class OverflowProviderTurn:
    error: ContextOverflowError
    started: bool
    content: str
    started_response_id: str | None
    had_streamed_delta: bool


@dataclass(frozen=True, slots=True)
class CancelledProviderTurn:
    pass


type ProviderTurnOutcome = (
    CompletedProviderTurn | FailedProviderTurn | OverflowProviderTurn | CancelledProviderTurn
)


def _is_cancelled(config: AgentLoopConfig) -> bool:
    token = config.cancellation_token
    return token is not None and token.is_cancelled()


def _cancelled_turn_events(turn: int) -> tuple[ErrorEvent, TurnCompleted]:
    return (
        ErrorEvent(message="Agent run cancelled"),
        TurnCompleted(turn=turn, outcome="cancelled", finish_reason="cancelled"),
    )


@dataclass(slots=True)
class ProviderTurn:
    """Stream one provider sample and record a typed terminal outcome."""

    config: AgentLoopConfig
    messages: Sequence[Message]
    tool_results: Sequence[ToolCallResult]
    extra_messages: Sequence[Message]
    previous_response_id: str | None
    outcome: ProviderTurnOutcome | None = None

    async def events(self, *, turn: int) -> AsyncIterator[ProviderTurnEvent]:
        lifecycle = ProviderResponseLifecycle()
        attempt_had_streamed_delta = False
        try:
            provider_stream = open_provider_stream(
                self.config,
                messages=self.messages,
                tool_results=self.tool_results,
                extra_messages=self.extra_messages,
                previous_response_id=self.previous_response_id,
            )
            async for provider_event in iter_provider_events(provider_stream):
                if _is_cancelled(self.config):
                    for event in _cancelled_turn_events(turn):
                        yield event
                    self.outcome = CancelledProviderTurn()
                    return
                lifecycle.require_open()
                if isinstance(provider_event, ProviderResponseStarted):
                    lifecycle.start(provider_event)
                    yield MessageStarted(turn=turn)
                elif isinstance(provider_event, provider_events.ProviderRetrying):
                    lifecycle.retry()
                    yield ProviderRetrying(
                        turn=turn,
                        provider=self.config.provider.name,
                        attempt=provider_event.attempt,
                        max_attempts=provider_event.max_attempts,
                        delay_seconds=provider_event.delay_seconds,
                        reason=provider_event.reason,
                        status_code=provider_event.status_code,
                    )
                elif isinstance(provider_event, ProviderTextDelta):
                    lifecycle.add_text(provider_event.delta)
                    attempt_had_streamed_delta = True
                    yield MessageDelta(
                        turn=turn,
                        delta=provider_event.delta,
                        content_index=provider_event.content_index,
                    )
                elif isinstance(provider_event, ProviderThinkingDelta):
                    lifecycle.add_thinking(provider_event.delta)
                    attempt_had_streamed_delta = True
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
                    event_type = type(provider_event).__name__
                    raise ProviderProtocolError(
                        f"Provider emitted unsupported event type: {event_type}"
                    )
        except ContextOverflowError as exc:
            self.outcome = OverflowProviderTurn(
                error=exc,
                started=lifecycle.started,
                content="".join(lifecycle.text),
                started_response_id=lifecycle.started_response_id,
                had_streamed_delta=attempt_had_streamed_delta,
            )
            return
        except Exception as exc:
            if is_context_overflow_message(str(exc)):
                self.outcome = OverflowProviderTurn(
                    error=ContextOverflowError(str(exc)),
                    started=lifecycle.started,
                    content="".join(lifecycle.text),
                    started_response_id=lifecycle.started_response_id,
                    had_streamed_delta=attempt_had_streamed_delta,
                )
                return
            raise

        if _is_cancelled(self.config):
            for event in _cancelled_turn_events(turn):
                yield event
            self.outcome = CancelledProviderTurn()
            return
        finished = lifecycle.finish()
        if isinstance(finished, FailedProviderResponse):
            self.outcome = FailedProviderTurn(
                failed=finished,
                started=lifecycle.started,
                had_streamed_delta=attempt_had_streamed_delta,
            )
            return
        self.outcome = CompletedProviderTurn(
            completed=finished,
            had_streamed_delta=attempt_had_streamed_delta,
        )
