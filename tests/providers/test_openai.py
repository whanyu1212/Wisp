from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import cast

import anyio
import httpx
import pytest
from openai import APIConnectionError
from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseCreatedEvent,
    ResponseError,
    ResponseErrorEvent,
    ResponseFailedEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseFunctionToolCall,
    ResponseIncompleteEvent,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseRefusalDeltaEvent,
    ResponseStreamEvent,
)
from openai.types.responses.response import IncompleteDetails

from tests.providers.openai_support import (
    StubOpenAIProvider,
    completed_event,
    stub_response,
    text_delta,
)
from wisp.agent.messages import Message
from wisp.providers.base import (
    ToolCall,
    ToolCallResult,
    ToolSpec,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderToolCallCompleted,
)
from wisp.providers.openai import OpenAIProvider


class FailingOpenAIProvider(OpenAIProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test-key", default_model="default-test-model")

    async def _create_stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[ResponseStreamEvent]:
        async def stream() -> AsyncIterator[ResponseStreamEvent]:
            yield text_delta("partial")
            raise APIConnectionError(request=httpx.Request("POST", "https://api.openai.com"))

        return stream()


def test_openai_provider_streams_text_deltas() -> None:
    provider = StubOpenAIProvider(
        [
            text_delta("hello"),
            text_delta(" world", content_index=1, sequence_number=1),
            completed_event(),
        ]
    )
    messages = [Message(role="user", content="Say hello")]

    async def run() -> list[object]:
        return [event async for event in provider.stream(messages, model="gpt-test")]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="gpt-test"),
        ProviderTextDelta(delta="hello"),
        ProviderTextDelta(delta=" world", content_index=1),
        ProviderResponseCompleted(content="hello world", response_id="response-id"),
    ]
    assert provider.seen_model == "gpt-test"
    assert provider.seen_messages == messages


def test_openai_provider_closes_stream_after_native_completion() -> None:
    provider = StubOpenAIProvider([completed_event(), text_delta("must not be consumed")])

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hello")])]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderResponseCompleted(content="", response_id="response-id"),
    ]
    assert provider.created_stream is not None
    assert provider.created_stream.closed


def test_openai_provider_uses_default_model_when_model_is_not_provided() -> None:
    provider = StubOpenAIProvider([text_delta("hello"), completed_event()])

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hello")])]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderTextDelta(delta="hello"),
        ProviderResponseCompleted(content="hello", response_id="response-id"),
    ]
    assert provider.seen_model == "default-test-model"


def test_openai_provider_uses_buffered_item_metadata_for_argument_done_events() -> None:
    provider = StubOpenAIProvider(
        [
            _function_call_output_item_added_event("lookup"),
            _function_call_arguments_done_event("wrong-name", '{"query": "wisp"}'),
            completed_event(),
        ]
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hello")])]

    events = anyio.run(run)

    tool_call = ToolCall(
        call_id="call-id",
        name="lookup",
        arguments={"query": "wisp"},
        raw_arguments='{"query": "wisp"}',
    )
    assert events == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderToolCallCompleted(tool_call=tool_call),
        ProviderResponseCompleted(
            content="",
            tool_calls=(tool_call,),
            response_id="response-id",
            finish_reason="tool_calls",
        ),
    ]


def test_openai_provider_waits_for_output_item_done_when_arguments_done_lacks_metadata() -> None:
    provider = StubOpenAIProvider(
        [
            _function_call_arguments_done_event_without_name('{"query": "wisp"}'),
            _function_call_output_item_done_event("lookup", arguments="{}"),
            completed_event(),
        ]
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hello")])]

    events = anyio.run(run)

    tool_call = ToolCall(
        call_id="call-id",
        name="lookup",
        arguments={"query": "wisp"},
        raw_arguments='{"query": "wisp"}',
    )
    assert events == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderToolCallCompleted(tool_call=tool_call),
        ProviderResponseCompleted(
            content="",
            tool_calls=(tool_call,),
            response_id="response-id",
            finish_reason="tool_calls",
        ),
    ]


def test_openai_provider_streams_function_tool_calls() -> None:
    provider = StubOpenAIProvider(
        [
            _created_event("response-id"),
            _function_call_output_item_done_event("lookup", arguments='{"query": "wisp"}'),
            completed_event(),
        ]
    )
    tool = ToolSpec(
        name="lookup",
        description="Look something up.",
        input_schema={"type": "object", "properties": {}},
    )

    async def run() -> list[object]:
        return [
            event
            async for event in provider.stream(
                [Message(role="user", content="hello")],
                tools=[tool],
            )
        ]

    events = anyio.run(run)

    tool_call = ToolCall(
        call_id="call-id",
        name="lookup",
        arguments={"query": "wisp"},
        raw_arguments='{"query": "wisp"}',
        response_id="response-id",
    )
    assert events == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderToolCallCompleted(tool_call=tool_call),
        ProviderResponseCompleted(
            content="",
            tool_calls=(tool_call,),
            response_id="response-id",
            finish_reason="tool_calls",
        ),
    ]


def test_openai_provider_streams_tool_call_parse_errors() -> None:
    provider = StubOpenAIProvider(
        [
            _function_call_output_item_added_event("lookup"),
            _function_call_arguments_done_event("lookup", "not-json"),
            completed_event(),
        ]
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hello")])]

    events = anyio.run(run)

    tool_call = ToolCall(
        call_id="call-id",
        name="lookup",
        arguments={},
        raw_arguments="not-json",
        parse_error="Invalid JSON arguments for tool lookup: Expecting value",
    )
    assert events == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderToolCallCompleted(tool_call=tool_call),
        ProviderResponseCompleted(
            content="",
            tool_calls=(tool_call,),
            response_id="response-id",
            finish_reason="tool_calls",
        ),
    ]


def test_openai_provider_streams_refusal_deltas() -> None:
    provider = StubOpenAIProvider(
        [_refusal_delta("I can't help with that", content_index=2), completed_event()]
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hello")])]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderTextDelta(delta="I can't help with that", content_index=2),
        ProviderResponseCompleted(content="I can't help with that", response_id="response-id"),
    ]


@pytest.mark.production_fault
def test_openai_provider_rejects_eof_without_native_completion() -> None:
    provider = StubOpenAIProvider([_created_event("response-id"), text_delta("partial")])

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hello")])]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderTextDelta(delta="partial"),
        ProviderResponseFailed(
            message="OpenAI stream ended before response.completed was received",
            partial_content="partial",
            response_id="response-id",
        ),
    ]


def test_openai_provider_does_not_expose_tool_calls_before_native_completion() -> None:
    provider = StubOpenAIProvider(
        [
            _created_event("response-id"),
            _function_call_output_item_done_event("lookup", arguments='{"query": "wisp"}'),
        ]
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hello")])]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderResponseFailed(
            message="OpenAI stream ended before response.completed was received",
            response_id="response-id",
        ),
    ]


def test_openai_provider_emits_failed_terminal_on_stream_error_event() -> None:
    provider = StubOpenAIProvider([_error_event("boom")])

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hello")])]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderResponseFailed(message="OpenAI API error: boom"),
    ]


def test_openai_provider_emits_failed_terminal_on_failed_response_event() -> None:
    provider = StubOpenAIProvider([_failed_event("server exploded")])

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hello")])]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderResponseFailed(message="OpenAI response failed: server exploded"),
    ]


def test_openai_provider_emits_failed_terminal_on_incomplete_response_event() -> None:
    provider = StubOpenAIProvider([_incomplete_event("max_output_tokens")])

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hello")])]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderResponseFailed(message="OpenAI response incomplete: max_output_tokens"),
    ]


def test_openai_provider_normalizes_post_start_sdk_failure() -> None:
    provider = FailingOpenAIProvider()

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hello")])]

    events = anyio.run(run)

    assert events[:2] == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderTextDelta(delta="partial"),
    ]
    assert events[2] == ProviderResponseFailed(
        message="OpenAI stream error: Connection error.",
        partial_content="partial",
    )


def test_openai_provider_rejects_idless_server_side_continuation() -> None:
    """An old Responses cursor cannot stand in for the current response ID."""

    response_without_id = stub_response(response_id="response-id").model_copy(update={"id": None})
    completion_without_id = cast(
        ResponseCompletedEvent,
        completed_event().model_copy(update={"response": response_without_id}),
    )
    provider = StubOpenAIProvider([completion_without_id])

    async def run() -> list[object]:
        return [
            event
            async for event in provider.stream(
                [Message(role="user", content="hello")],
                previous_response_id="previous-response",
            )
        ]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderResponseFailed(
            message="OpenAI continuation response did not include a response id"
        ),
    ]


def _refusal_delta(
    text: str,
    *,
    content_index: int = 0,
    sequence_number: int = 0,
) -> ResponseRefusalDeltaEvent:
    return ResponseRefusalDeltaEvent(
        content_index=content_index,
        delta=text,
        item_id="item",
        output_index=0,
        sequence_number=sequence_number,
        type="response.refusal.delta",
    )


def _created_event(response_id: str) -> ResponseCreatedEvent:
    return ResponseCreatedEvent(
        response=stub_response(response_id=response_id),
        sequence_number=0,
        type="response.created",
    )


def _function_call_arguments_done_event(
    name: str,
    arguments: str = "{}",
) -> ResponseFunctionCallArgumentsDoneEvent:
    return ResponseFunctionCallArgumentsDoneEvent(
        arguments=arguments,
        item_id="item",
        name=name,
        output_index=0,
        sequence_number=0,
        type="response.function_call_arguments.done",
    )


def _function_call_arguments_done_event_without_name(
    arguments: str,
) -> ResponseFunctionCallArgumentsDoneEvent:
    return ResponseFunctionCallArgumentsDoneEvent.model_construct(
        arguments=arguments,
        item_id="item",
        name=None,
        output_index=0,
        sequence_number=0,
        type="response.function_call_arguments.done",
    )


def _function_call_output_item_added_event(
    name: str,
    *,
    arguments: str = "",
) -> ResponseOutputItemAddedEvent:
    return ResponseOutputItemAddedEvent(
        item=ResponseFunctionToolCall(
            arguments=arguments,
            call_id="call-id",
            id="item",
            name=name,
            type="function_call",
        ),
        output_index=0,
        sequence_number=0,
        type="response.output_item.added",
    )


def _function_call_output_item_done_event(
    name: str,
    *,
    arguments: str = "{}",
) -> ResponseOutputItemDoneEvent:
    return ResponseOutputItemDoneEvent(
        item=ResponseFunctionToolCall(
            arguments=arguments,
            call_id="call-id",
            id="item",
            name=name,
            type="function_call",
        ),
        output_index=0,
        sequence_number=0,
        type="response.output_item.done",
    )


def _error_event(message: str) -> ResponseErrorEvent:
    return ResponseErrorEvent(message=message, sequence_number=0, type="error")


def _failed_event(message: str) -> ResponseFailedEvent:
    error = ResponseError(code="server_error", message=message)
    response = stub_response(error=error)
    return ResponseFailedEvent(response=response, sequence_number=0, type="response.failed")


def _incomplete_event(reason: str) -> ResponseIncompleteEvent:
    response = stub_response(incomplete_details=IncompleteDetails(reason=reason))
    return ResponseIncompleteEvent(response=response, sequence_number=0, type="response.incomplete")
