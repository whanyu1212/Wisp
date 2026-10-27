from __future__ import annotations

import json
from typing import cast

import anyio
import httpx
import pytest
from openai import AsyncOpenAI

from tests.providers.openai_support import (
    StubAsyncOpenAI,
    StubOpenAIProvider,
    StubResponsesResource,
    completed_event,
    text_delta,
)
from wisp.events import ToolCallSnapshot
from wisp.messages import Message
from wisp.providers.base import (
    ToolCallResult,
    ToolSpec,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderTextDelta,
)
from wisp.providers.openai import OpenAIProvider


def test_openai_provider_accepts_provider_tool_specs() -> None:
    provider = StubOpenAIProvider([text_delta("hello"), completed_event()])
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


def test_openai_provider_stream_forwards_effort_and_cache_key() -> None:
    provider = StubOpenAIProvider([text_delta("hi"), completed_event()])

    async def run() -> list[object]:
        return [
            event
            async for event in provider.stream(
                [Message(role="user", content="hello")],
                effort="low",
                prompt_cache_key="wisp:session-1",
            )
        ]

    anyio.run(run)

    assert provider.seen_effort == "low"
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
