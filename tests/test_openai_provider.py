from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

import anyio
import httpx
import pytest
from openai import APIConnectionError, AsyncOpenAI
from openai.types.responses import (
    Response,
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
    ResponseTextDeltaEvent,
)
from openai.types.responses.response import IncompleteDetails
from pytest import MonkeyPatch

from wisp.agent.messages import Message
from wisp.providers.base import (
    ProviderConfigurationError,
    ToolCall,
    ToolCallResult,
    ToolSpec,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderRetrying,
    ProviderTextDelta,
    ProviderToolCallCompleted,
)
from wisp.providers.openai import OpenAIProvider
from wisp.retry import RetryPolicy


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
        self.seen_model = model
        self.seen_messages = messages
        self.seen_tools = tools
        self.seen_tool_results = tool_results
        self.seen_previous_response_id = previous_response_id
        self.seen_effort = effort

        async def stream() -> AsyncIterator[ResponseStreamEvent]:
            for event in self.events:
                yield event

        return stream()


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
            yield _text_delta("partial")
            raise APIConnectionError(request=httpx.Request("POST", "https://api.openai.com"))

        return stream()


class FlakyOpenAIProvider(OpenAIProvider):
    def __init__(self, failures: int, *, retry_policy: RetryPolicy) -> None:
        super().__init__(
            api_key="test-key",
            default_model="default-test-model",
            retry_policy=retry_policy,
        )
        self.failures = failures
        self.attempts = 0

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
        self.attempts += 1
        if self.attempts <= self.failures:
            raise APIConnectionError(request=httpx.Request("POST", "https://api.openai.com"))

        async def stream() -> AsyncIterator[ResponseStreamEvent]:
            yield _text_delta("recovered")
            yield _completed_event()

        return stream()


def test_openai_provider_streams_text_deltas() -> None:
    provider = StubOpenAIProvider(
        [
            _text_delta("hello"),
            _text_delta(" world", content_index=1, sequence_number=1),
            _completed_event(),
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


def test_openai_provider_uses_default_model_when_model_is_not_provided() -> None:
    provider = StubOpenAIProvider([_text_delta("hello"), _completed_event()])

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hello")])]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderTextDelta(delta="hello"),
        ProviderResponseCompleted(content="hello", response_id="response-id"),
    ]
    assert provider.seen_model == "default-test-model"


def test_openai_provider_accepts_provider_tool_specs() -> None:
    provider = StubOpenAIProvider([_text_delta("hello"), _completed_event()])
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

    assert anyio.run(run) == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderTextDelta(delta="hello"),
        ProviderResponseCompleted(content="hello", response_id="response-id"),
    ]
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


def test_openai_provider_sends_reasoning_effort_when_provided() -> None:
    responses = StubResponsesResource()
    provider = OpenAIProvider(
        api_key="test-key",
        client=cast(AsyncOpenAI, StubAsyncOpenAI(responses)),
    )

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [Message(role="user", content="hello")],
            model="gpt-test",
            effort="high",
        )
        assert [event async for event in stream] == []

    anyio.run(run)

    assert responses.calls == [
        {
            "model": "gpt-test",
            "input": [{"role": "user", "content": "hello"}],
            "stream": True,
            "reasoning": {"effort": "high"},
        }
    ]


def test_openai_provider_omits_reasoning_when_effort_is_not_provided() -> None:
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

    assert "reasoning" not in responses.calls[0]


def test_openai_provider_stream_forwards_effort_to_create_stream() -> None:
    provider = StubOpenAIProvider([_text_delta("hi"), _completed_event()])

    async def run() -> list[object]:
        return [
            event
            async for event in provider.stream(
                [Message(role="user", content="hello")], effort="low"
            )
        ]

    anyio.run(run)

    assert provider.seen_effort == "low"


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
            _completed_event(),
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
            _completed_event(),
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
            _completed_event(),
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
            _completed_event(),
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
        [_refusal_delta("I can't help with that", content_index=2), _completed_event()]
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hello")])]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderTextDelta(delta="I can't help with that", content_index=2),
        ProviderResponseCompleted(content="I can't help with that", response_id="response-id"),
    ]


def test_openai_provider_rejects_eof_without_native_completion() -> None:
    provider = StubOpenAIProvider([_created_event("response-id"), _text_delta("partial")])

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


def test_openai_provider_retries_request_opening_failure_before_start() -> None:
    provider = FlakyOpenAIProvider(
        1,
        retry_policy=RetryPolicy(max_retries=1, base_delay_seconds=0.0001, max_delay_seconds=1),
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hello")])]

    events = anyio.run(run)

    assert provider.attempts == 2
    assert isinstance(events[0], ProviderRetrying)
    assert events[0].attempt == 2
    assert events[0].max_attempts == 2
    assert events[0].reason == "network"
    assert events[1:] == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderTextDelta(delta="recovered"),
        ProviderResponseCompleted(content="recovered", response_id="response-id"),
    ]


def test_openai_provider_raises_after_exhausting_opening_retries() -> None:
    provider = FlakyOpenAIProvider(
        2,
        retry_policy=RetryPolicy(max_retries=1, base_delay_seconds=0.0001, max_delay_seconds=1),
    )

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(APIConnectionError):
            async for event in provider.stream([Message(role="user", content="hello")]):
                events.append(event)
        return events

    events = anyio.run(run)

    assert provider.attempts == 2
    assert len(events) == 1
    assert isinstance(events[0], ProviderRetrying)


def test_openai_provider_stops_retrying_when_cancelled_during_backoff() -> None:
    provider = FlakyOpenAIProvider(
        1,
        retry_policy=RetryPolicy(max_retries=1, base_delay_seconds=5, max_delay_seconds=5),
    )

    async def run() -> None:
        stream = provider.stream([Message(role="user", content="hello")])
        assert isinstance(await anext(stream), ProviderRetrying)
        with anyio.move_on_after(0.01) as scope:
            await anext(stream)
        assert scope.cancel_called
        assert provider.attempts == 1

    anyio.run(run)


def test_wisp_owned_openai_client_disables_sdk_retries() -> None:
    provider = OpenAIProvider(api_key="test-key")

    assert provider._client_or_create().max_retries == 0  # noqa: SLF001


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


def _text_delta(
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
        response=_response(response_id=response_id),
        sequence_number=0,
        type="response.created",
    )


def _completed_event(response_id: str = "response-id") -> ResponseCompletedEvent:
    return ResponseCompletedEvent(
        response=_response(response_id=response_id),
        sequence_number=0,
        type="response.completed",
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
