"""Stub clients and Responses stream events shared by the OpenAI provider tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseError,
    ResponseStreamEvent,
    ResponseTextDeltaEvent,
)
from openai.types.responses.response import IncompleteDetails

from wisp.agent.messages import Message
from wisp.providers.base import (
    ToolCallResult,
    ToolSpec,
)
from wisp.providers.openai import OpenAIProvider


class _ClosableStubStream:
    def __init__(self, events: Sequence[ResponseStreamEvent]) -> None:
        self._events = iter(events)
        self.closed = False

    def __aiter__(self) -> _ClosableStubStream:
        return self

    async def __anext__(self) -> ResponseStreamEvent:
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration from None

    async def close(self) -> None:
        self.closed = True


class StubOpenAIProvider(OpenAIProvider):
    def __init__(self, events: Sequence[ResponseStreamEvent]) -> None:
        super().__init__(api_key="test-key", default_model="default-test-model")
        self.events = events
        self.seen_model: str | None = None
        self.seen_messages: Sequence[Message] | None = None
        self.seen_tools: Sequence[ToolSpec] | None = None
        self.seen_tool_results: Sequence[ToolCallResult] | None = None
        self.seen_previous_response_id: str | None = None
        self.seen_effort: str | None = None
        self.seen_prompt_cache_key: str | None = None
        self.created_stream: _ClosableStubStream | None = None

    async def _create_stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
        prompt_cache_key: str | None = None,
    ) -> AsyncIterator[ResponseStreamEvent]:
        self.seen_model = model
        self.seen_messages = messages
        self.seen_tools = tools
        self.seen_tool_results = tool_results
        self.seen_previous_response_id = previous_response_id
        self.seen_effort = effort
        self.seen_prompt_cache_key = prompt_cache_key

        self.created_stream = _ClosableStubStream(self.events)
        return self.created_stream


class StubAsyncOpenAI:
    def __init__(self, responses: StubResponsesResource) -> None:
        self.responses = responses


class StubResponsesResource:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: object) -> AsyncIterator[ResponseStreamEvent]:
        self.calls.append(dict(kwargs))

        async def stream() -> AsyncIterator[ResponseStreamEvent]:
            if False:
                yield text_delta("unreachable")

        return stream()


def text_delta(
    text: str,
    *,
    content_index: int = 0,
    sequence_number: int = 0,
) -> ResponseTextDeltaEvent:
    return ResponseTextDeltaEvent(
        content_index=content_index,
        delta=text,
        item_id="item",
        logprobs=[],
        output_index=0,
        sequence_number=sequence_number,
        type="response.output_text.delta",
    )


def completed_event(response_id: str = "response-id") -> ResponseCompletedEvent:
    return ResponseCompletedEvent(
        response=stub_response(response_id=response_id),
        sequence_number=0,
        type="response.completed",
    )


def stub_response(
    *,
    response_id: str = "response-id",
    error: ResponseError | None = None,
    incomplete_details: IncompleteDetails | None = None,
) -> Response:
    return Response(
        id=response_id,
        created_at=0.0,
        error=error,
        incomplete_details=incomplete_details,
        model="gpt-5.5",
        object="response",
        output=[],
        parallel_tool_calls=True,
        tool_choice="auto",
        tools=[],
    )
