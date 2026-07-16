from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

import anyio
import httpx
import pytest
from anthropic import APIConnectionError, AsyncAnthropic
from anthropic.types import (
    InputJSONDelta,
    Message,
    MessageDeltaUsage,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawMessageDeltaEvent,
    RawMessageStartEvent,
    RawMessageStreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolUseBlock,
    Usage,
)
from anthropic.types.raw_content_block_start_event import ContentBlock
from anthropic.types.raw_message_delta_event import Delta
from pytest import MonkeyPatch

from wisp.agent.messages import Message as WispMessage
from wisp.providers.anthropic import AnthropicProvider
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
    ProviderThinkingDelta,
    ProviderToolCallCompleted,
)
from wisp.retry import RetryPolicy


class StubAnthropicProvider(AnthropicProvider):
    def __init__(self, events: Sequence[RawMessageStreamEvent]) -> None:
        super().__init__(api_key="test-key", default_model="default-test-model")
        self.events = events
        self.seen_model: str | None = None
        self.seen_messages: Sequence[WispMessage] | None = None
        self.seen_tools: Sequence[ToolSpec] | None = None
        self.seen_tool_results: Sequence[ToolCallResult] | None = None

    async def _create_stream(
        self,
        messages: Sequence[WispMessage],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
    ) -> AsyncIterator[RawMessageStreamEvent]:
        self.seen_model = model
        self.seen_messages = messages
        self.seen_tools = tools
        self.seen_tool_results = tool_results

        async def stream() -> AsyncIterator[RawMessageStreamEvent]:
            for event in self.events:
                yield event

        return stream()


class FailingAnthropicProvider(AnthropicProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test-key", default_model="default-test-model")

    async def _create_stream(
        self,
        messages: Sequence[WispMessage],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
    ) -> AsyncIterator[RawMessageStreamEvent]:
        async def stream() -> AsyncIterator[RawMessageStreamEvent]:
            yield _text_delta("partial")
            raise APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))

        return stream()


class FlakyAnthropicProvider(AnthropicProvider):
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
        messages: Sequence[WispMessage],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
    ) -> AsyncIterator[RawMessageStreamEvent]:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))

        async def stream() -> AsyncIterator[RawMessageStreamEvent]:
            yield _text_delta("recovered")

        return stream()


def test_anthropic_provider_streams_text_deltas() -> None:
    provider = StubAnthropicProvider(
        [
            _message_start("response-id"),
            _text_delta("hello"),
            _text_delta(" world", index=1),
            _message_delta("end_turn"),
        ]
    )
    messages = [WispMessage(role="user", content="Say hello")]

    async def run() -> list[object]:
        return [event async for event in provider.stream(messages, model="claude-test")]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="claude-test"),
        ProviderTextDelta(delta="hello"),
        ProviderTextDelta(delta=" world", content_index=1),
        ProviderResponseCompleted(content="hello world", response_id="response-id"),
    ]
    assert provider.seen_model == "claude-test"
    assert provider.seen_messages == messages


def test_anthropic_provider_uses_default_model_when_model_is_not_provided() -> None:
    provider = StubAnthropicProvider([_text_delta("hello")])

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderTextDelta(delta="hello"),
        ProviderResponseCompleted(content="hello"),
    ]
    assert provider.seen_model == "default-test-model"


def test_anthropic_provider_streams_thinking_deltas() -> None:
    provider = StubAnthropicProvider(
        [
            _thinking_delta("let me think", index=0),
            _text_delta("the answer", index=1),
        ]
    )

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderThinkingDelta(delta="let me think", content_index=0),
        ProviderTextDelta(delta="the answer", content_index=1),
        ProviderResponseCompleted(content="the answer"),
    ]


def test_anthropic_provider_streams_tool_calls() -> None:
    provider = StubAnthropicProvider(
        [
            _tool_use_start("call-id", "lookup", index=0),
            _input_json_delta('{"query": ', index=0),
            _input_json_delta('"wisp"}', index=0),
            _message_delta("tool_use"),
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
                [WispMessage(role="user", content="hello")],
                tools=[tool],
            )
        ]

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
            finish_reason="tool_calls",
        ),
    ]
    assert provider.seen_tools == [tool]


def test_anthropic_provider_streams_tool_call_parse_errors() -> None:
    provider = StubAnthropicProvider(
        [
            _tool_use_start("call-id", "lookup", index=0),
            _input_json_delta("not-json", index=0),
            _message_delta("tool_use"),
        ]
    )

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

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
            finish_reason="tool_calls",
        ),
    ]


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("pause_turn", "stop"),
        ("refusal", "stop"),
        ("max_tokens", "length"),
    ],
)
def test_anthropic_provider_maps_stop_reasons_to_finish_reasons(
    stop_reason: str, expected: str
) -> None:
    provider = StubAnthropicProvider([_text_delta("hi"), _message_delta(stop_reason)])

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    events = anyio.run(run)
    completed = events[-1]
    assert isinstance(completed, ProviderResponseCompleted)
    assert completed.finish_reason == expected


def test_anthropic_provider_emits_failed_terminal_on_stream_error() -> None:
    provider = FailingAnthropicProvider()

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    events = anyio.run(run)

    assert events[:2] == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderTextDelta(delta="partial"),
    ]
    assert events[2] == ProviderResponseFailed(
        message="Anthropic stream error: Connection error.",
        partial_content="partial",
    )


def test_anthropic_provider_retries_request_opening_failure_before_start() -> None:
    provider = FlakyAnthropicProvider(
        1,
        retry_policy=RetryPolicy(max_retries=1, base_delay_seconds=0.0001, max_delay_seconds=1),
    )

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    events = anyio.run(run)

    assert provider.attempts == 2
    assert isinstance(events[0], ProviderRetrying)
    assert events[0].attempt == 2
    assert events[0].max_attempts == 2
    assert events[0].reason == "network"
    assert events[1:] == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderTextDelta(delta="recovered"),
        ProviderResponseCompleted(content="recovered"),
    ]


def test_anthropic_provider_raises_after_exhausting_opening_retries() -> None:
    provider = FlakyAnthropicProvider(
        2,
        retry_policy=RetryPolicy(max_retries=1, base_delay_seconds=0.0001, max_delay_seconds=1),
    )

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(APIConnectionError):
            async for event in provider.stream([WispMessage(role="user", content="hello")]):
                events.append(event)
        return events

    events = anyio.run(run)

    assert provider.attempts == 2
    assert len(events) == 1
    assert isinstance(events[0], ProviderRetrying)


def test_wisp_owned_anthropic_client_disables_sdk_retries() -> None:
    provider = AnthropicProvider(api_key="test-key")

    assert provider._client_or_create().max_retries == 0  # noqa: SLF001


def test_anthropic_provider_requires_api_key(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = AnthropicProvider()

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    with pytest.raises(ProviderConfigurationError, match="ANTHROPIC_API_KEY is required"):
        anyio.run(run)


def test_anthropic_provider_splits_system_messages_from_conversation() -> None:
    messages_resource = StubMessagesResource()
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
    )

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [
                WispMessage(role="system", content="instructions"),
                WispMessage(role="system", content="context"),
                WispMessage(role="user", content="hello"),
            ],
            model="claude-test",
        )
        assert [event async for event in stream] == []

    anyio.run(run)

    assert messages_resource.calls == [
        {
            "model": "claude-test",
            "max_tokens": 64000,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
            "stream": True,
            "system": "instructions\n\ncontext",
        }
    ]


def test_anthropic_provider_omits_system_when_no_system_messages() -> None:
    messages_resource = StubMessagesResource()
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
    )

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [WispMessage(role="user", content="hello")],
            model="claude-test",
        )
        assert [event async for event in stream] == []

    anyio.run(run)

    assert messages_resource.calls == [
        {
            "model": "claude-test",
            "max_tokens": 64000,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
            "stream": True,
        }
    ]


def test_anthropic_provider_folds_tool_results_into_one_trailing_user_message() -> None:
    messages_resource = StubMessagesResource()
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
    )
    tool_results = [
        ToolCallResult(call_id="call-1", output="result one"),
        ToolCallResult(call_id="call-2", output="failed", is_error=True),
    ]

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [WispMessage(role="assistant", content="using tools")],
            model="claude-test",
            tool_results=tool_results,
        )
        assert [event async for event in stream] == []

    anyio.run(run)

    assert messages_resource.calls == [
        {
            "model": "claude-test",
            "max_tokens": 64000,
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "using tools"}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call-1",
                            "content": "result one",
                            "is_error": False,
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "call-2",
                            "content": "failed",
                            "is_error": True,
                        },
                    ],
                },
            ],
            "stream": True,
        }
    ]


def test_anthropic_provider_serializes_tool_specs() -> None:
    messages_resource = StubMessagesResource()
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
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
            [WispMessage(role="user", content="hello")],
            model="claude-test",
            tools=[tool],
        )
        assert [event async for event in stream] == []

    anyio.run(run)

    assert messages_resource.calls == [
        {
            "model": "claude-test",
            "max_tokens": 64000,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
            "stream": True,
            "tools": [
                {
                    "name": "lookup",
                    "description": "Look something up.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                }
            ],
        }
    ]


class StubAsyncAnthropic:
    def __init__(self, messages: StubMessagesResource) -> None:
        self.messages = messages


class StubMessagesResource:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: object) -> AsyncIterator[RawMessageStreamEvent]:
        self.calls.append(dict(kwargs))

        async def stream() -> AsyncIterator[RawMessageStreamEvent]:
            if False:
                yield _text_delta("unreachable")

        return stream()


def _message_start(response_id: str) -> RawMessageStartEvent:
    return RawMessageStartEvent(
        message=Message(
            id=response_id,
            content=[],
            model="claude-test",
            role="assistant",
            type="message",
            usage=Usage(input_tokens=1, output_tokens=0),
        ),
        type="message_start",
    )


def _text_delta(text: str, *, index: int = 0) -> RawContentBlockDeltaEvent:
    return RawContentBlockDeltaEvent(
        delta=TextDelta(text=text, type="text_delta"),
        index=index,
        type="content_block_delta",
    )


def _thinking_delta(text: str, *, index: int = 0) -> RawContentBlockDeltaEvent:
    return RawContentBlockDeltaEvent(
        delta=ThinkingDelta(thinking=text, type="thinking_delta"),
        index=index,
        type="content_block_delta",
    )


def _tool_use_start(call_id: str, name: str, *, index: int) -> RawContentBlockStartEvent:
    content_block = cast(
        ContentBlock,
        ToolUseBlock(id=call_id, input={}, name=name, type="tool_use"),
    )
    return RawContentBlockStartEvent(
        content_block=content_block,
        index=index,
        type="content_block_start",
    )


def _input_json_delta(partial_json: str, *, index: int) -> RawContentBlockDeltaEvent:
    return RawContentBlockDeltaEvent(
        delta=InputJSONDelta(partial_json=partial_json, type="input_json_delta"),
        index=index,
        type="content_block_delta",
    )


def _message_delta(stop_reason: str) -> RawMessageDeltaEvent:
    return RawMessageDeltaEvent(
        delta=Delta.model_construct(stop_reason=stop_reason),
        type="message_delta",
        usage=MessageDeltaUsage(output_tokens=1),
    )
