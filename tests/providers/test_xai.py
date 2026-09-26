from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import cast

import anyio
import pytest
from openai import AsyncOpenAI
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseReasoningItem,
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseReasoningTextDeltaEvent,
    ResponseStreamEvent,
    ResponseTextDeltaEvent,
)
from pytest import MonkeyPatch

from wisp.agent.messages import Message, NativeOutput
from wisp.providers.base import ProviderConfigurationError, ToolCallResult, ToolSpec
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderThinkingDelta,
)
from wisp.providers.openai import OpenAIProvider
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
            "include": ["reasoning.encrypted_content"],
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


class _ScriptedResponses:
    """Return one completed response per request, recording each request."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> AsyncIterator[ResponseStreamEvent]:
        self.calls.append(dict(kwargs))
        response_id = f"resp-{len(self.calls)}"

        async def stream() -> AsyncIterator[ResponseStreamEvent]:
            yield _text_delta("hello")
            yield _completed_with_output(response_id)

        return stream()


_REPLAYED_ITEMS = [
    {"summary": [], "type": "reasoning", "encrypted_content": "enc"},
    {
        "content": [{"annotations": [], "text": "hello", "type": "output_text"}],
        "role": "assistant",
        "status": "completed",
        "type": "message",
    },
]


def _completed_with_output(response_id: str) -> ResponseCompletedEvent:
    event = _completed_event()
    response = event.response.model_copy(
        update={
            "id": response_id,
            "output": [
                ResponseReasoningItem(
                    id="rs-1", type="reasoning", summary=[], encrypted_content="enc"
                ),
                ResponseOutputMessage(
                    id="msg-1",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[ResponseOutputText(type="output_text", text="hello", annotations=[])],
                ),
            ],
        }
    )
    return event.model_copy(update={"response": response})


def _follow_up_history(native_output: NativeOutput | None = None) -> list[Message]:
    return [
        Message(role="user", content="hi"),
        Message(
            role="assistant", content="hello", response_id="resp-1", native_output=native_output
        ),
        Message(role="user", content="next"),
    ]


def _run_prompts(provider: OpenAIProvider, *prompts: tuple[list[Message], str]) -> None:
    async def run() -> None:
        for messages, model in prompts:
            async for _event in provider.stream(messages, model=model):
                pass

    anyio.run(run)


def test_xai_fresh_request_replays_the_previous_responses_output_items() -> None:
    responses = _ScriptedResponses()
    provider = XAIProvider(client=cast(AsyncOpenAI, _StubClient(responses)))

    _run_prompts(
        provider,
        ([Message(role="user", content="hi")], "grok-4.5"),
        (_follow_up_history(), "grok-4.5"),
        (_follow_up_history(), "grok-4.6"),
    )

    assert responses.calls[0]["include"] == ["reasoning.encrypted_content"]
    assert responses.calls[1]["input"] == [
        {"role": "user", "content": "hi"},
        *_REPLAYED_ITEMS,
        {"role": "user", "content": "next"},
    ]
    # Encrypted reasoning belongs to the model that produced it.
    assert responses.calls[2]["input"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "next"},
    ]
    native = provider.native_output_for("resp-1")
    assert native is not None
    assert (native.provider, native.model) == ("xai", "grok-4.5")


def test_xai_replays_items_saved_on_the_row_from_a_new_provider_instance() -> None:
    responses = _ScriptedResponses()
    provider = XAIProvider(client=cast(AsyncOpenAI, _StubClient(responses)))
    saved = NativeOutput(provider="xai", model="grok-4.5", items=tuple(_REPLAYED_ITEMS))

    _run_prompts(provider, (_follow_up_history(saved), "grok-4.5"))

    assert responses.calls[0]["input"] == [
        {"role": "user", "content": "hi"},
        *_REPLAYED_ITEMS,
        {"role": "user", "content": "next"},
    ]


def test_openai_provider_keeps_the_portable_rebuild_until_replay_is_enabled() -> None:
    responses = _ScriptedResponses()
    provider = OpenAIProvider(client=cast(AsyncOpenAI, _StubClient(responses)))
    saved = NativeOutput(provider="openai", model="gpt-test", items=tuple(_REPLAYED_ITEMS))

    _run_prompts(
        provider,
        ([Message(role="user", content="hi")], "gpt-test"),
        (_follow_up_history(saved), "gpt-test"),
    )

    assert all("include" not in call for call in responses.calls)
    assert responses.calls[1]["input"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "next"},
    ]
    assert provider.native_output_for("resp-1") is None
