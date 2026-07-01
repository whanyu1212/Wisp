from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

import anyio
import pytest
from openai import AsyncOpenAI
from openai.types.responses import (
    Response,
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
    ResponseTextDeltaEvent,
)
from openai.types.responses.response import IncompleteDetails
from pytest import MonkeyPatch

from wisp.agent.messages import Message
from wisp.providers.base import (
    ProviderConfigurationError,
    ProviderError,
    ToolCall,
    ToolCallResult,
    ToolSpec,
)
from wisp.providers.openai import OpenAIProvider


class StubOpenAIProvider(OpenAIProvider):
    def __init__(self, events: Sequence[ResponseStreamEvent]) -> None:
        super().__init__(api_key="test-key", default_model="default-test-model")
        self.events = events
        self.seen_model: str | None = None
        self.seen_messages: Sequence[Message] | None = None
        self.seen_tools: Sequence[ToolSpec] | None = None
        self.seen_tool_results: Sequence[ToolCallResult] | None = None
        self.seen_previous_response_id: str | None = None

    async def _create_stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
    ) -> AsyncIterator[ResponseStreamEvent]:
        self.seen_model = model
        self.seen_messages = messages
        self.seen_tools = tools
        self.seen_tool_results = tool_results
        self.seen_previous_response_id = previous_response_id

        async def stream() -> AsyncIterator[ResponseStreamEvent]:
            for event in self.events:
                yield event

        return stream()


def test_openai_provider_streams_text_deltas() -> None:
    provider = StubOpenAIProvider(
        [
            _text_delta("hello"),
            _text_delta(" world", sequence_number=1),
        ]
    )
    messages = [Message(role="user", content="Say hello")]

    async def run() -> list[str]:
        return [delta async for delta in provider.stream(messages, model="gpt-test")]

    assert anyio.run(run) == ["hello", " world"]
    assert provider.seen_model == "gpt-test"
    assert provider.seen_messages == messages


def test_openai_provider_uses_default_model_when_model_is_not_provided() -> None:
    provider = StubOpenAIProvider([_text_delta("hello")])

    async def run() -> list[str]:
        return [delta async for delta in provider.stream([Message(role="user", content="hello")])]

    assert anyio.run(run) == ["hello"]
    assert provider.seen_model == "default-test-model"


def test_openai_provider_accepts_provider_tool_specs() -> None:
    provider = StubOpenAIProvider([_text_delta("hello")])
    tool = ToolSpec(
        name="lookup",
        description="Look something up.",
        input_schema={"type": "object", "properties": {}},
    )

    async def run() -> list[str]:
        return [
            delta
            async for delta in provider.stream(
                [Message(role="user", content="hello")],
                tools=[tool],
            )
        ]

    assert anyio.run(run) == ["hello"]
    assert provider.seen_tools == [tool]


def test_openai_provider_serializes_tool_specs_to_responses_tools() -> None:
    responses = StubResponsesResource()
    provider = OpenAIProvider(
        api_key="test-key",
        client=cast(AsyncOpenAI, StubAsyncOpenAI(responses)),
    )
    tool = ToolSpec(
        name="lookup",
        description="Look something up.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [Message(role="user", content="hello")],
            model="gpt-test",
            tools=[tool],
        )
        assert [event async for event in stream] == []

    anyio.run(run)

    assert responses.calls == [
        {
            "model": "gpt-test",
            "input": [{"role": "user", "content": "hello"}],
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "Look something up.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                    "strict": False,
                }
            ],
        }
    ]


def test_openai_provider_omits_tools_when_no_tool_specs_are_provided() -> None:
    responses = StubResponsesResource()
    provider = OpenAIProvider(
        api_key="test-key",
        client=cast(AsyncOpenAI, StubAsyncOpenAI(responses)),
    )

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [Message(role="user", content="hello")],
            model="gpt-test",
        )
        assert [event async for event in stream] == []

    anyio.run(run)

    assert responses.calls == [
        {
            "model": "gpt-test",
            "input": [{"role": "user", "content": "hello"}],
            "stream": True,
        }
    ]


def test_openai_provider_serializes_system_context_before_user_message() -> None:
    responses = StubResponsesResource()
    provider = OpenAIProvider(
        api_key="test-key",
        client=cast(AsyncOpenAI, StubAsyncOpenAI(responses)),
    )

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [
                Message(role="system", content="instructions"),
                Message(role="system", content="context"),
                Message(role="user", content="hello"),
            ],
            model="gpt-test",
        )
        assert [event async for event in stream] == []

    anyio.run(run)

    assert responses.calls == [
        {
            "model": "gpt-test",
            "input": [
                {"role": "system", "content": "instructions"},
                {"role": "system", "content": "context"},
                {"role": "user", "content": "hello"},
            ],
            "stream": True,
        }
    ]


def test_openai_provider_sends_tool_results_with_previous_response_id() -> None:
    responses = StubResponsesResource()
    provider = OpenAIProvider(
        api_key="test-key",
        client=cast(AsyncOpenAI, StubAsyncOpenAI(responses)),
    )
    tool = ToolSpec(
        name="lookup",
        description="Look something up.",
        input_schema={"type": "object", "properties": {}},
    )
    tool_result = ToolCallResult(call_id="call-id", output="found it")

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [Message(role="user", content="hello")],
            model="gpt-test",
            tools=[tool],
            tool_results=[tool_result],
            previous_response_id="response-id",
        )
        assert [event async for event in stream] == []

    anyio.run(run)

    assert responses.calls == [
        {
            "model": "gpt-test",
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": "call-id",
                    "output": "found it",
                }
            ],
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "Look something up.",
                    "parameters": {"type": "object", "properties": {}},
                    "strict": False,
                }
            ],
            "previous_response_id": "response-id",
        }
    ]


def test_openai_provider_uses_buffered_item_metadata_for_argument_done_events() -> None:
    provider = StubOpenAIProvider(
        [
            _function_call_output_item_added_event("lookup"),
            _function_call_arguments_done_event("wrong-name", '{"query": "wisp"}'),
        ]
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hello")])]

    events = anyio.run(run)

    assert events == [
        ToolCall(
            call_id="call-id",
            name="lookup",
            arguments={"query": "wisp"},
            raw_arguments='{"query": "wisp"}',
        )
    ]


def test_openai_provider_waits_for_output_item_done_when_arguments_done_lacks_metadata() -> None:
    provider = StubOpenAIProvider(
        [
            _function_call_arguments_done_event_without_name('{"query": "wisp"}'),
            _function_call_output_item_done_event("lookup", arguments="{}"),
        ]
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hello")])]

    events = anyio.run(run)

    assert events == [
        ToolCall(
            call_id="call-id",
            name="lookup",
            arguments={"query": "wisp"},
            raw_arguments='{"query": "wisp"}',
        )
    ]


def test_openai_provider_streams_function_tool_calls() -> None:
    provider = StubOpenAIProvider(
        [
            _created_event("response-id"),
            _function_call_output_item_done_event("lookup", arguments='{"query": "wisp"}'),
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

    assert events == [
        ToolCall(
            call_id="call-id",
            name="lookup",
            arguments={"query": "wisp"},
            raw_arguments='{"query": "wisp"}',
            response_id="response-id",
        )
    ]


def test_openai_provider_streams_tool_call_parse_errors() -> None:
    provider = StubOpenAIProvider(
        [
            _function_call_output_item_added_event("lookup"),
            _function_call_arguments_done_event("lookup", "not-json"),
        ]
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hello")])]

    events = anyio.run(run)

    assert events == [
        ToolCall(
            call_id="call-id",
            name="lookup",
            arguments={},
            raw_arguments="not-json",
            parse_error="Invalid JSON arguments for tool lookup: Expecting value",
        )
    ]


def test_openai_provider_streams_refusal_deltas() -> None:
    provider = StubOpenAIProvider([_refusal_delta("I can't help with that")])

    async def run() -> list[str]:
        return [delta async for delta in provider.stream([Message(role="user", content="hello")])]

    assert anyio.run(run) == ["I can't help with that"]


def test_openai_provider_raises_on_stream_error_event() -> None:
    provider = StubOpenAIProvider([_error_event("boom")])

    async def run() -> list[str]:
        return [delta async for delta in provider.stream([Message(role="user", content="hello")])]

    with pytest.raises(ProviderError, match="OpenAI API error: boom"):
        anyio.run(run)


def test_openai_provider_raises_on_failed_response_event() -> None:
    provider = StubOpenAIProvider([_failed_event("server exploded")])

    async def run() -> list[str]:
        return [delta async for delta in provider.stream([Message(role="user", content="hello")])]

    with pytest.raises(ProviderError, match="OpenAI response failed: server exploded"):
        anyio.run(run)


def test_openai_provider_raises_on_incomplete_response_event() -> None:
    provider = StubOpenAIProvider([_incomplete_event("max_output_tokens")])

    async def run() -> list[str]:
        return [delta async for delta in provider.stream([Message(role="user", content="hello")])]

    with pytest.raises(ProviderError, match="OpenAI response incomplete: max_output_tokens"):
        anyio.run(run)


def test_openai_provider_requires_api_key(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAIProvider()

    async def run() -> list[str]:
        return [delta async for delta in provider.stream([Message(role="user", content="hello")])]

    with pytest.raises(ProviderConfigurationError, match="OPENAI_API_KEY is required"):
        anyio.run(run)


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
                yield _text_delta("unreachable")

        return stream()


def _text_delta(text: str, *, sequence_number: int = 0) -> ResponseTextDeltaEvent:
    return ResponseTextDeltaEvent(
        content_index=0,
        delta=text,
        item_id="item",
        logprobs=[],
        output_index=0,
        sequence_number=sequence_number,
        type="response.output_text.delta",
    )


def _refusal_delta(text: str, *, sequence_number: int = 0) -> ResponseRefusalDeltaEvent:
    return ResponseRefusalDeltaEvent(
        content_index=0,
        delta=text,
        item_id="item",
        output_index=0,
        sequence_number=sequence_number,
        type="response.refusal.delta",
    )


def _created_event(response_id: str) -> ResponseCreatedEvent:
    return ResponseCreatedEvent(
        response=_response(response_id=response_id),
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
    response = _response(error=error)
    return ResponseFailedEvent(response=response, sequence_number=0, type="response.failed")


def _incomplete_event(reason: str) -> ResponseIncompleteEvent:
    response = _response(incomplete_details=IncompleteDetails(reason=reason))
    return ResponseIncompleteEvent(response=response, sequence_number=0, type="response.incomplete")


def _response(
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
