"""Stream one model response and project its completion metadata."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

import wisp.providers.events as provider_events
from wisp.agent.context_budget import observe_context
from wisp.agent.messages import Message
from wisp.events import (
    ErrorEvent,
    MessageCompleted,
    MessageDelta,
    MessageStarted,
    ProviderRetrying,
    TokenUsage,
    ToolCallSnapshot,
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
)

from .continuation import provider_supports_continuation_messages
from .provider_lifecycle import (
    CompletedProviderResponse,
    FailedProviderResponse,
    ProviderResponseLifecycle,
)
from .stream_cleanup import closing_stream

if TYPE_CHECKING:
    from .config import AgentLoopConfig

type ModelResponseEvent = (
    MessageStarted | ProviderRetrying | MessageDelta | ErrorEvent | TurnCompleted
)


def provider_supports_prompt_cache_key(provider: Provider) -> bool:
    """Check whether an adapter explicitly accepts a prompt cache key.

    Args:
        provider (Provider): Adapter whose optional capability is inspected.

    Returns:
        bool: True only when supports_prompt_cache_key is literally True.
    """
    return getattr(provider, "supports_prompt_cache_key", False) is True


def open_provider_stream(
    config: AgentLoopConfig,
    *,
    messages: Sequence[Message],
    tool_results: Sequence[ToolCallResult],
    extra_messages: Sequence[Message],
    previous_response_id: str | None,
) -> AsyncIterator[provider_events.ProviderEvent]:
    """Open a provider stream using only the optional keywords it supports.

    Args:
        config (AgentLoopConfig): Provider, model, tool schemas, effort, and cache settings.
        messages (Sequence[Message]): Portable base history.
        tool_results (Sequence[ToolCallResult]): Pending outputs for the next request.
        extra_messages (Sequence[Message]): User messages for a continued request; sent
            only when the adapter advertises continuation-message support.
        previous_response_id (str | None): Native continuation cursor, if available.

    Returns:
        AsyncIterator[provider_events.ProviderEvent]: Provider stream, not yet consumed.
            The caller owns its lifetime and must close it when supported. Effort is omitted
            when unset; cache keys are sent only to supporting adapters.

    Raises:
        Exception: Synchronous errors from the provider's stream factory propagate.
            Errors while consuming the returned iterator occur later.
    """

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
    """Forward provider events, normalize overflow errors, and close the owned iterator.

    Args:
        stream (AsyncIterator[provider_events.ProviderEvent]): Provider event source.

    Yields:
        provider_events.ProviderEvent: Each provider event unchanged and in source order.

    Raises:
        ContextOverflowError: Advancing the iterator raises an error whose message
            matches a known overflow pattern before a terminal event; the original
            error is chained as its cause.
        Exception: Non-overflow iterator errors and post-terminal cleanup errors
            propagate unchanged.
    """

    terminal_received = False
    async with closing_stream(aiter(stream)) as iterator:
        while True:
            try:
                event = await anext(iterator)
            except StopAsyncIteration:
                return
            except Exception as exc:
                # Async-generator finally/aclose errors surface on the anext after
                # the last event. They are not a rejected request.
                if terminal_received or not is_context_overflow_message(str(exc)):
                    raise
                raise ContextOverflowError(str(exc)) from exc
            if isinstance(event, ProviderResponseCompleted | ProviderResponseFailed):
                terminal_received = True
            yield event


def unavailable_cost(
    provider: str,
    requested_model: str | None,
    response_model: str | None,
    *,
    reason: Literal["pricing_unavailable", "usage_incomplete", "estimation_failed"],
) -> UsageCost:
    """Build cost metadata that explains why a price could not be calculated.

    Args:
        provider (str): Provider identifier.
        requested_model (str | None): Model requested by the caller.
        response_model (str | None): Model reported by the provider, if available.
        reason (Literal["pricing_unavailable", "usage_incomplete", "estimation_failed"]):
            Reason pricing is unavailable.

    Returns:
        UsageCost: Unpriced metadata using the reported model, or requested model as fallback.
    """

    return UsageCost(
        provider=provider,
        requested_model=requested_model,
        model=response_model or requested_model,
        unavailable_reason=reason,
    )


def project_usage_and_cost(
    config: AgentLoopConfig,
    completed: CompletedProviderResponse,
) -> tuple[TokenUsage | None, UsageCost]:
    """Project usage and price a completed response without failing on missing pricing.

    Args:
        config (AgentLoopConfig): Provider identity, requested model, and cost estimator.
        completed (CompletedProviderResponse): Validated response with optional token usage.

    Returns:
        tuple[TokenUsage | None, UsageCost]: Usage when reported, plus priced or explicitly
            unavailable cost metadata. Estimator exceptions become estimation_failed.
    """

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
class CompletedResponseProjection:
    """Public completion plus tool snapshots isolated before the public yield."""

    event: MessageCompleted
    continuation_tool_calls: tuple[ToolCallSnapshot, ...]

    def to_continuation_message(self) -> Message:
        """Build the live transcript row after the completion consumer resumes.

        Returns:
            Message: Assistant row created now, using completion metadata and the tool
                snapshots isolated before the public event was yielded.
        """

        event = self.event
        return Message(
            role="assistant",
            content=event.content,
            response_id=event.response_id,
            finish_reason=event.finish_reason,
            usage=event.usage,
            cost=event.cost,
            context_observation=event.context_observation,
            tool_calls=self.continuation_tool_calls,
        )


def project_completed_response(
    config: AgentLoopConfig,
    completed: CompletedProviderResponse,
    *,
    turn: int,
    request_messages: Sequence[Message],
    selected_model: str | None,
) -> CompletedResponseProjection:
    """Prepare public completion metadata and isolated continuation tool snapshots.

    Args:
        config (AgentLoopConfig): Provider, tools, and optional usage-pricing callback.
        completed (CompletedProviderResponse): Validated successful response.
        turn (int): Turn number attached to the public completion event.
        request_messages (Sequence[Message]): Exact portable context used for observation.
        selected_model (str | None): Requested model or provider default for the observation.

    Returns:
        CompletedResponseProjection: Public event and deep-copied continuation tool
            snapshots. The continuation Message is constructed later by the caller.

    Examples:
        Preserve tool arguments across a consumer-controlled yield boundary::

            projection = project_completed_response(
                config, completed, turn=turn,
                request_messages=request_messages, selected_model=selected_model,
            )
            yield projection.event
            continuation = projection.to_continuation_message()

        The observation prefers context_input_tokens when reported. Pressure events
        are calculated separately by the runner from usage.input_tokens.
    """

    response = completed.response
    usage, cost = project_usage_and_cost(config, completed)
    tool_call_snapshots = tuple(
        ToolCallSnapshot(
            call_id=tool_call.call_id,
            name=tool_call.name,
            arguments=deepcopy(dict(tool_call.arguments)),
            provider_call_id=tool_call.provider_call_id,
            parse_error=tool_call.parse_error,
        )
        for tool_call in response.tool_calls
    )
    # Copy before yielding: public arguments can contain mutable nested values.
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
                if response.usage is not None and response.usage.context_input_tokens is not None
                else usage.input_tokens
            ),
        )
        if usage is not None
        else None
    )
    return CompletedResponseProjection(
        event=MessageCompleted(
            turn=turn,
            content=completed.content,
            finish_reason=response.finish_reason,
            response_id=completed.response_id,
            usage=usage,
            cost=cost,
            context_observation=context_observation,
            tool_calls=tool_call_snapshots,
        ),
        continuation_tool_calls=continuation_tool_calls,
    )


@dataclass(frozen=True, slots=True)
class CompletedModelResponse:
    """Represent a successful stream with its validated response and delta-presence flag."""

    completed: CompletedProviderResponse
    had_streamed_delta: bool


@dataclass(frozen=True, slots=True)
class FailedModelResponse:
    """Represent a typed provider failure, including whether its message lifecycle opened."""

    failed: FailedProviderResponse
    started: bool
    had_streamed_delta: bool


@dataclass(frozen=True, slots=True)
class OverflowModelResponse:
    """Represent a raised overflow with partial text and lifecycle state for recovery."""

    error: ContextOverflowError
    started: bool
    content: str
    started_response_id: str | None
    had_streamed_delta: bool


@dataclass(frozen=True, slots=True)
class CancelledModelResponse:
    """Mark a stream that already emitted its turn-cancellation events."""

    pass


type ModelResponseOutcome = (
    CompletedModelResponse | FailedModelResponse | OverflowModelResponse | CancelledModelResponse
)


def _is_cancelled(config: AgentLoopConfig) -> bool:
    """Read the optional cooperative cancellation token.

    Args:
        config (AgentLoopConfig): Configuration for the active stream.

    Returns:
        bool: True when cancellation is requested; False when no token is configured.
    """
    token = config.cancellation_token
    return token is not None and token.is_cancelled()


def _cancelled_turn_events(turn: int) -> tuple[ErrorEvent, TurnCompleted]:
    """Build cancellation events for the stream's active turn.

    Args:
        turn (int): Number of the turn being cancelled.

    Returns:
        tuple[ErrorEvent, TurnCompleted]: Error followed by cancelled turn completion.
    """
    return (
        ErrorEvent(message="Agent run cancelled"),
        TurnCompleted(turn=turn, outcome="cancelled", finish_reason="cancelled"),
    )


@dataclass(slots=True)
class ModelResponseStream:
    """Stream one model response and retain its typed terminal outcome.

    Args:
        config (AgentLoopConfig): Provider and run settings.
        messages (Sequence[Message]): Portable base history.
        tool_results (Sequence[ToolCallResult]): Results pending delivery to the model.
        extra_messages (Sequence[Message]): User messages queued for this continuation.
        previous_response_id (str | None): Native provider cursor, if available.
        outcome (ModelResponseOutcome | None, optional): Terminal result, initially None.
            Normally left unset and populated by consuming events to completion.
    """

    config: AgentLoopConfig
    messages: Sequence[Message]
    tool_results: Sequence[ToolCallResult]
    extra_messages: Sequence[Message]
    previous_response_id: str | None
    outcome: ModelResponseOutcome | None = None

    async def events(self, *, turn: int) -> AsyncIterator[ModelResponseEvent]:
        """Translate provider progress and record a terminal outcome when exhausted.

        Args:
            turn (int): Active turn number for all public events.

        Yields:
            ModelResponseEvent: Start, text/thinking delta, and retry events, plus terminal
                turn events on cancellation. Successful MessageCompleted publication
                belongs to the runner, which first prepares usage and continuation data.

        Examples:
            Given a configured ModelResponseStream, consume it before inspecting outcome::

                async def collect_response(stream, turn):
                    events = [event async for event in stream.events(turn=turn)]
                    return events, stream.outcome

        Raises:
            ProviderProtocolError: Events violate lifecycle, identity, or tool-call rules.
            Exception: Unexpected provider or token errors propagate. Context overflow
                during consumption becomes OverflowModelResponse; typed provider failure
                becomes FailedModelResponse. Cleanup errors after a recorded terminal,
                consumer closure, and cancellation keep their original exception.
                An interrupted consumer may leave outcome unset.
        """
        lifecycle = ProviderResponseLifecycle()
        attempt_had_streamed_delta = False
        consumer_closed = False
        try:
            provider_stream = open_provider_stream(
                self.config,
                messages=self.messages,
                tool_results=self.tool_results,
                extra_messages=self.extra_messages,
                previous_response_id=self.previous_response_id,
            )
            async with closing_stream(
                iter_provider_events(provider_stream)
            ) as provider_events_stream:
                try:
                    async for provider_event in provider_events_stream:
                        if _is_cancelled(self.config):
                            for event in _cancelled_turn_events(turn):
                                yield event
                            self.outcome = CancelledModelResponse()
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
                        elif isinstance(
                            provider_event, ProviderResponseCompleted | ProviderResponseFailed
                        ):
                            lifecycle.complete(provider_event)
                        else:
                            event_type = type(provider_event).__name__
                            raise ProviderProtocolError(
                                f"Provider emitted unsupported event type: {event_type}"
                            )
                except GeneratorExit:
                    consumer_closed = True
                    raise
        except Exception as exc:
            # A recorded terminal means the provider already finished; aclose
            # failures must not be rewritten into overflow recovery.
            if (
                consumer_closed
                or isinstance(self.outcome, CancelledModelResponse)
                or lifecycle.terminal is not None
            ):
                raise
            if isinstance(exc, ContextOverflowError):
                overflow_error = exc
            elif is_context_overflow_message(str(exc)):
                overflow_error = ContextOverflowError(str(exc))
            else:
                raise
            self.outcome = OverflowModelResponse(
                error=overflow_error,
                started=lifecycle.started,
                content="".join(lifecycle.text),
                started_response_id=lifecycle.started_response_id,
                had_streamed_delta=attempt_had_streamed_delta,
            )
            return

        if _is_cancelled(self.config):
            for event in _cancelled_turn_events(turn):
                yield event
            self.outcome = CancelledModelResponse()
            return
        finished = lifecycle.finish()
        if isinstance(finished, FailedProviderResponse):
            self.outcome = FailedModelResponse(
                failed=finished,
                started=lifecycle.started,
                had_streamed_delta=attempt_had_streamed_delta,
            )
            return
        self.outcome = CompletedModelResponse(
            completed=finished,
            had_streamed_delta=attempt_had_streamed_delta,
        )
