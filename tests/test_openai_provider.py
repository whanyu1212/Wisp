from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
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
from wisp.auth.storage import ApiKeyCredential, JsonAuthStore
from wisp.events import ToolCallSnapshot
from wisp.providers.auth import StoredProviderAuthResolver
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


def test_openai_provider_closes_stream_after_native_completion() -> None:
    provider = StubOpenAIProvider([_completed_event(), _text_delta("must not be consumed")])

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hello")])]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderResponseCompleted(content="", response_id="response-id"),
    ]
    assert provider.created_stream is not None
    assert provider.created_stream.closed


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


def test_openai_provider_sends_prompt_cache_key_when_provided() -> None:
    responses = StubResponsesResource()
    provider = OpenAIProvider(
        api_key="test-key",
        client=cast(AsyncOpenAI, StubAsyncOpenAI(responses)),
    )

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [Message(role="user", content="hello")],
            model="gpt-test",
            prompt_cache_key="wisp:session-1",
        )
        assert [event async for event in stream] == []

    anyio.run(run)

    assert responses.calls[0]["prompt_cache_key"] == "wisp:session-1"


def test_openai_provider_omits_prompt_cache_key_when_not_provided() -> None:
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

    assert "prompt_cache_key" not in responses.calls[0]


def test_openai_provider_uses_explicit_cache_for_gpt_5_6_stable_prefix() -> None:
    responses = StubResponsesResource()
    provider = OpenAIProvider(
        api_key="test-key",
        client=cast(AsyncOpenAI, StubAsyncOpenAI(responses)),
    )

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [
                Message(
                    role="system",
                    content="stable core",
                    prompt_cache_boundary=True,
                ),
                Message(role="system", content="changing project context"),
                Message(role="user", content="current task"),
            ],
            model="gpt-5.6-sol",
            prompt_cache_key="wisp:session-1",
        )
        assert [event async for event in stream] == []

    anyio.run(run)

    assert responses.calls[0]["input"] == [
        {
            "role": "system",
            "content": [
                {
                    "type": "input_text",
                    "text": "stable core",
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                }
            ],
        },
        {"role": "system", "content": "changing project context"},
        {"role": "user", "content": "current task"},
    ]
    assert responses.calls[0]["extra_body"] == {"prompt_cache_options": {"mode": "explicit"}}


def test_openai_sdk_preserves_forward_compatible_explicit_cache_fields() -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(cast(dict[str, object], json.loads((await request.aread()).decode())))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"data: [DONE]\n\n",
        )

    async def run() -> None:
        client = AsyncOpenAI(
            api_key="test-key",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        try:
            provider = OpenAIProvider(client=client)
            stream = await provider._create_stream(  # noqa: SLF001
                [
                    Message(
                        role="system",
                        content="stable",
                        prompt_cache_boundary=True,
                    ),
                    Message(role="user", content="dynamic"),
                ],
                model="gpt-5.6-sol",
                prompt_cache_key="wisp:session-1",
            )
            assert [event async for event in stream] == []
        finally:
            await client.close()

    anyio.run(run)

    assert requests[0]["prompt_cache_options"] == {"mode": "explicit"}
    assert cast(list[dict[str, object]], requests[0]["input"])[0]["content"] == [
        {
            "type": "input_text",
            "text": "stable",
            "prompt_cache_breakpoint": {"mode": "explicit"},
        }
    ]


@pytest.mark.parametrize("model", ["gpt-5.5", "future-model"])
def test_openai_provider_keeps_legacy_cache_shape_for_unsupported_models(model: str) -> None:
    responses = StubResponsesResource()
    provider = OpenAIProvider(
        api_key="test-key",
        client=cast(AsyncOpenAI, StubAsyncOpenAI(responses)),
    )

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [Message(role="system", content="stable", prompt_cache_boundary=True)],
            model=model,
            prompt_cache_key="wisp:session-1",
        )
        assert [event async for event in stream] == []

    anyio.run(run)

    assert responses.calls[0]["input"] == [{"role": "system", "content": "stable"}]
    assert "extra_body" not in responses.calls[0]


def test_openai_provider_keeps_legacy_shape_without_key_or_boundary() -> None:
    responses = StubResponsesResource()
    provider = OpenAIProvider(
        api_key="test-key",
        client=cast(AsyncOpenAI, StubAsyncOpenAI(responses)),
    )

    async def run() -> None:
        without_key = await provider._create_stream(  # noqa: SLF001
            [Message(role="system", content="stable", prompt_cache_boundary=True)],
            model="gpt-5.6-sol",
        )
        assert [event async for event in without_key] == []
        without_boundary = await provider._create_stream(  # noqa: SLF001
            [Message(role="system", content="stable")],
            model="gpt-5.6-sol",
            prompt_cache_key="wisp:session-1",
        )
        assert [event async for event in without_boundary] == []

    anyio.run(run)

    assert all("extra_body" not in call for call in responses.calls)


def test_openai_provider_keeps_explicit_policy_on_tool_result_continuation() -> None:
    responses = StubResponsesResource()
    provider = OpenAIProvider(
        api_key="test-key",
        client=cast(AsyncOpenAI, StubAsyncOpenAI(responses)),
    )

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [Message(role="system", content="stable", prompt_cache_boundary=True)],
            model="gpt-5.6-sol",
            tool_results=[ToolCallResult(call_id="call-id", output="found")],
            previous_response_id="response-id",
            prompt_cache_key="wisp:session-1",
        )
        assert [event async for event in stream] == []

    anyio.run(run)

    assert responses.calls[0]["input"] == [
        {"type": "function_call_output", "call_id": "call-id", "output": "found"}
    ]
    assert responses.calls[0]["extra_body"] == {"prompt_cache_options": {"mode": "explicit"}}


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


def test_openai_provider_stream_forwards_prompt_cache_key_to_create_stream() -> None:
    provider = StubOpenAIProvider([_text_delta("hi"), _completed_event()])

    async def run() -> list[object]:
        return [
            event
            async for event in provider.stream(
                [Message(role="user", content="hello")],
                prompt_cache_key="wisp:session-1",
            )
        ]

    anyio.run(run)

    assert provider.seen_prompt_cache_key == "wisp:session-1"


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
            extra_messages=[Message(role="user", content="steered")],
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
                },
                {"role": "user", "content": "steered"},
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


def test_openai_provider_serializes_active_tool_exchange_in_fresh_context() -> None:
    responses = StubResponsesResource()
    provider = OpenAIProvider(
        api_key="test-key",
        client=cast(AsyncOpenAI, StubAsyncOpenAI(responses)),
    )
    messages = [
        Message(role="user", content="search"),
        Message(
            role="assistant",
            content="checking",
            tool_calls=(
                ToolCallSnapshot(call_id="call-1", name="lookup", arguments={"query": "wisp"}),
            ),
        ),
        Message(role="tool", content="found it", tool_call_id="call-1", tool_name="lookup"),
    ]

    async def run() -> None:
        stream = await provider._create_stream(messages, model="gpt-test")  # noqa: SLF001
        assert [event async for event in stream] == []

    anyio.run(run)

    assert responses.calls[0]["input"] == [
        {"role": "user", "content": "search"},
        {"role": "assistant", "content": "checking"},
        {
            "type": "function_call",
            "call_id": "call-1",
            "name": "lookup",
            "arguments": '{"query":"wisp"}',
        },
        {"type": "function_call_output", "call_id": "call-1", "output": "found it"},
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


def test_openai_provider_rejects_idless_server_side_continuation() -> None:
    """An old Responses cursor cannot stand in for the current response ID."""

    response_without_id = _response(response_id="response-id").model_copy(update={"id": None})
    completion_without_id = cast(
        ResponseCompletedEvent,
        _completed_event().model_copy(update={"response": response_without_id}),
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
        return [event async for event in provider.stream([Message(role="user", content="hello")])]

    events = anyio.run(run)

    assert provider.attempts == 2
    assert len(events) == 2
    assert isinstance(events[0], ProviderRetrying)
    assert isinstance(events[1], ProviderResponseFailed)


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

    async def run() -> None:
        client = await provider._client_or_create()  # noqa: SLF001
        assert client.max_retries == 0
        await client.close()

    anyio.run(run)


def test_openai_provider_uses_and_rotates_stored_api_key(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store = JsonAuthStore(tmp_path / "auth.json")
    store.set("openai", ApiKeyCredential(key="old-stored-key"))
    provider = OpenAIProvider(auth_resolver=StoredProviderAuthResolver(store))

    async def run() -> None:
        old_client = await provider._client_or_create()  # noqa: SLF001
        store.set("openai", ApiKeyCredential(key="new-stored-key"))
        new_client = await provider._client_or_create()  # noqa: SLF001

        assert old_client.api_key == "old-stored-key"
        assert new_client.api_key == "new-stored-key"
        assert new_client is not old_client
        assert old_client.is_closed()
        await new_client.close()

    anyio.run(run)


def test_openai_provider_api_key_precedence(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    store = JsonAuthStore(tmp_path / "auth.json")
    store.set("openai", ApiKeyCredential(key="stored-key"))
    resolver = StoredProviderAuthResolver(store)
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")

    async def run() -> None:
        explicit_client = await OpenAIProvider(
            api_key="explicit-key", auth_resolver=resolver
        )._client_or_create()  # noqa: SLF001
        env_client = await OpenAIProvider(auth_resolver=resolver)._client_or_create()  # noqa: SLF001
        monkeypatch.delenv("OPENAI_API_KEY")
        stored_client = await OpenAIProvider(auth_resolver=resolver)._client_or_create()  # noqa: SLF001

        assert explicit_client.api_key == "explicit-key"
        assert env_client.api_key == "env-key"
        assert stored_client.api_key == "stored-key"
        await explicit_client.close()
        await env_client.close()
        await stored_client.close()

    anyio.run(run)


def test_openai_provider_does_not_read_store_for_higher_priority_api_keys(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text("{invalid", encoding="utf-8")
    resolver = StoredProviderAuthResolver(JsonAuthStore(auth_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def run() -> None:
        explicit_client = await OpenAIProvider(
            api_key="explicit-key", auth_resolver=resolver
        )._client_or_create()  # noqa: SLF001
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        env_client = await OpenAIProvider(auth_resolver=resolver)._client_or_create()  # noqa: SLF001

        assert explicit_client.api_key == "explicit-key"
        assert env_client.api_key == "env-key"
        await explicit_client.close()
        await env_client.close()

    anyio.run(run)


def test_openai_provider_does_not_replace_injected_client(tmp_path: Path) -> None:
    store = JsonAuthStore(tmp_path / "auth.json")
    store.set("openai", ApiKeyCredential(key="old-stored-key"))
    injected = cast(AsyncOpenAI, StubAsyncOpenAI(StubResponsesResource()))
    provider = OpenAIProvider(
        client=injected,
        auth_resolver=StoredProviderAuthResolver(store),
    )

    async def run() -> None:
        assert await provider._client_or_create() is injected  # noqa: SLF001
        store.set("openai", ApiKeyCredential(key="new-stored-key"))
        assert await provider._client_or_create() is injected  # noqa: SLF001

    anyio.run(run)


def test_openai_provider_requires_api_key(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAIProvider()

    async def run() -> list[str]:
        return [delta async for delta in provider.stream([Message(role="user", content="hello")])]

    with pytest.raises(ProviderConfigurationError, match=r"/connect.*OPENAI_API_KEY"):
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
