from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import cast

import anyio
import pytest
from openai import AsyncOpenAI
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseReasoningTextDeltaEvent,
    ResponseStreamEvent,
    ResponseTextDeltaEvent,
)
from pytest import MonkeyPatch

from wisp.agent.messages import Message
from wisp.providers.base import ProviderConfigurationError, ToolCallResult, ToolSpec
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderThinkingDelta,
)
from wisp.providers.xai import DEFAULT_XAI_MODEL, XAIProvider


class _StubResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> AsyncIterator[ResponseStreamEvent]:
        self.calls.append(dict(kwargs))

        async def stream() -> AsyncIterator[ResponseStreamEvent]:
            if False:
                yield _text_delta("unreachable")

        return stream()


class _StubClient:
    def __init__(self, responses: _StubResponses) -> None:
        self.responses = responses


class _StreamingXAIProvider(XAIProvider):
    def __init__(self, events: Sequence[ResponseStreamEvent]) -> None:
        super().__init__(api_key="test-key")
        self._events = events

    async def _create_stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        extra_messages: Sequence[Message] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
        prompt_cache_key: str | None = None,
    ) -> AsyncIterator[ResponseStreamEvent]:
        async def stream() -> AsyncIterator[ResponseStreamEvent]:
            for event in self._events:
                yield event

        return stream()


def test_xai_provider_uses_stateful_responses_request_and_native_continuation() -> None:
    responses = _StubResponses()
    provider = XAIProvider(client=cast(AsyncOpenAI, _StubClient(responses)))
    tool = ToolSpec(name="lookup", description="Look up", input_schema={"type": "object"})

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [Message(role="user", content="original")],
            model="grok-4.6",
            tools=(tool,),
            tool_results=(ToolCallResult(call_id="call-1", output="found"),),
            extra_messages=(Message(role="user", content="steer"),),
            previous_response_id="resp-1",
            effort="high",
        )
        assert [event async for event in stream] == []

    anyio.run(run)

    assert responses.calls == [
        {
            "model": "grok-4.6",
            "input": [
                {"type": "function_call_output", "call_id": "call-1", "output": "found"},
                {"role": "user", "content": "steer"},
            ],
            "stream": True,
            "store": True,
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "Look up",
                    "parameters": {"type": "object"},
                    "strict": False,
                }
            ],
            "previous_response_id": "resp-1",
            "reasoning": {"effort": "high"},
        }
    ]


def test_xai_provider_streams_text_and_both_reasoning_delta_types() -> None:
    provider = _StreamingXAIProvider(
        [
            ResponseReasoningTextDeltaEvent(
                content_index=0,
                delta="think ",
                item_id="reasoning-1",
                output_index=0,
                sequence_number=0,
                type="response.reasoning_text.delta",
            ),
            ResponseReasoningSummaryTextDeltaEvent(
                delta="summary",
                item_id="reasoning-1",
                output_index=0,
                sequence_number=1,
                summary_index=0,
                type="response.reasoning_summary_text.delta",
            ),
            _text_delta("answer"),
            _completed_event(),
        ]
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hi")])]

    assert anyio.run(run) == [
        ProviderResponseStarted(model=DEFAULT_XAI_MODEL),
        ProviderThinkingDelta(delta="think "),
        ProviderThinkingDelta(delta="summary"),
        ProviderTextDelta(delta="answer"),
        ProviderResponseCompleted(content="answer", response_id="resp-1"),
    ]


def test_xai_provider_creates_and_closes_client_for_xai_endpoint(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("XAI_API_KEY", "environment-key")
    provider = XAIProvider()

    async def run() -> tuple[str, bool]:
        client = await provider._client_or_create()  # noqa: SLF001
        base_url = str(client.base_url)
        await provider.aclose()
        return base_url, provider._client is None  # noqa: SLF001

    assert anyio.run(run) == ("https://api.x.ai/v1/", True)


def test_xai_provider_requires_xai_api_key(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    provider = XAIProvider()

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hi")])]

    with pytest.raises(ProviderConfigurationError, match=r"/connect xai.*XAI_API_KEY"):
        anyio.run(run)


def _text_delta(text: str) -> ResponseTextDeltaEvent:
    return ResponseTextDeltaEvent(
        content_index=0,
        delta=text,
        item_id="message-1",
        logprobs=[],
        output_index=0,
        sequence_number=2,
        type="response.output_text.delta",
    )


def _completed_event() -> ResponseCompletedEvent:
    return ResponseCompletedEvent(
        response=Response(
            id="resp-1",
            created_at=0.0,
            error=None,
            incomplete_details=None,
            model="grok-4.6",
            object="response",
            output=[],
            parallel_tool_calls=True,
            tool_choice="auto",
            tools=[],
        ),
        sequence_number=3,
        type="response.completed",
    )
