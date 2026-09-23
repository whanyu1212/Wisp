"""Translate one provider response lifecycle into typed loop outcomes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import wisp.providers.events as provider_events
from wisp.agent.messages import Message
from wisp.events import (
    MessageDelta,
    MessageStarted,
    ProviderRetrying,
)
from wisp.providers.base import (
    ContextOverflowError,
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

from .provider_lifecycle import (
    CompletedProviderResponse,
    FailedProviderResponse,
    ProviderResponseLifecycle,
)
from .provider_request import iter_provider_events, open_provider_stream
from .stream_cleanup import closing_stream

if TYPE_CHECKING:
    from .config import AgentLoopConfig


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
    """Ask the loop runner to publish cancellation terminals for this response."""

    pass


type ModelResponseOutcome = (
    CompletedModelResponse | FailedModelResponse | OverflowModelResponse | CancelledModelResponse
)
type ModelResponseEvent = MessageStarted | ProviderRetrying | MessageDelta | CancelledModelResponse


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
            ModelResponseEvent: Start, text/thinking delta, and retry events, plus an
                internal cancellation marker. Public completion and turn-terminal
                publication belong to the runner.

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
                        if self.config.cancellation_requested():
                            cancelled = CancelledModelResponse()
                            yield cancelled
                            self.outcome = cancelled
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

        if self.config.cancellation_requested():
            cancelled = CancelledModelResponse()
            yield cancelled
            self.outcome = cancelled
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
