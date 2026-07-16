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
    RedactedThinkingBlock,
    SignatureDelta,
    TextBlock,
    TextDelta,
    ThinkingBlock,
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
            yield _message_delta("end_turn")

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
    provider = StubAnthropicProvider([_text_delta("hello"), _message_delta("end_turn")])

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
            _message_delta("end_turn"),
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
            _message_start("response-id"),
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
    assert provider.seen_tools == [tool]


def test_anthropic_provider_streams_tool_call_parse_errors() -> None:
    provider = StubAnthropicProvider(
        [
            _message_start("response-id"),
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
        response_id="response-id",
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


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("refusal", "stop"),
        ("max_tokens", "length"),
        ("pause_turn", "length"),
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


def test_anthropic_provider_emits_failed_terminal_when_stream_ends_without_stop_reason() -> None:
    # Regression test: distinct from the exception-raising failure case above
    # -- here the async iterator ends *normally* (no exception), but a
    # dropped connection or truncated proxy response cut it off before any
    # message_delta ever carried a stop_reason. Silently falling through to
    # ProviderResponseCompleted(finish_reason="stop") would report a
    # truncated answer as a successful turn.
    provider = StubAnthropicProvider([_message_start("response-id"), _text_delta("partial")])

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
        message="Anthropic stream ended before a stop_reason was received",
        partial_content="partial",
        response_id="response-id",
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


def test_anthropic_provider_replays_tool_use_turn_before_tool_results() -> None:
    # Regression test: Anthropic's Messages API requires the assistant's
    # tool_use turn to immediately precede the matching tool_result -- but
    # AgentHarness's provider-neutral loop only ever passes the *new*
    # tool_results on a follow-up call, assuming a Responses-API-style
    # backend that remembers the prior turn server-side (previous_response_id
    # is otherwise unused, per stream()'s docstring). Without a replay, the
    # second request would 400 against the real API.
    messages_resource = StubMessagesResource(
        responses=[
            [
                _message_start("response-id"),
                _tool_use_start("call-id", "lookup", index=0),
                _input_json_delta('{"query": "wisp"}', index=0),
                _message_delta("tool_use"),
            ],
            [],
        ]
    )
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
    )
    tool = ToolSpec(
        name="lookup",
        description="Look something up.",
        input_schema={"type": "object", "properties": {}},
    )
    messages = [WispMessage(role="user", content="hello")]

    async def run() -> tuple[list[object], str | None]:
        first_events = [
            event async for event in provider.stream(messages, model="claude-test", tools=[tool])
        ]
        completed = first_events[-1]
        assert isinstance(completed, ProviderResponseCompleted)
        second_events = [
            event
            async for event in provider.stream(
                messages,
                model="claude-test",
                tools=[tool],
                tool_results=[ToolCallResult(call_id="call-id", output="found it")],
                previous_response_id=completed.response_id,
            )
        ]
        return second_events, completed.response_id

    anyio.run(run)

    assert len(messages_resource.calls) == 2
    second_call_messages = messages_resource.calls[1]["messages"]
    assert second_call_messages == [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call-id", "name": "lookup", "input": {"query": "wisp"}}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-id",
                    "content": "found it",
                    "is_error": False,
                }
            ],
        },
    ]


def test_anthropic_provider_replay_includes_text_alongside_tool_use() -> None:
    messages_resource = StubMessagesResource(
        responses=[
            [
                _message_start("response-id"),
                _text_block_start(index=0),
                _text_delta("Let me check.", index=0),
                _tool_use_start("call-id", "lookup", index=1),
                _input_json_delta("{}", index=1),
                _message_delta("tool_use"),
            ],
            [],
        ]
    )
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
    )
    messages = [WispMessage(role="user", content="hello")]

    async def run() -> None:
        first_events = [event async for event in provider.stream(messages, model="claude-test")]
        completed = first_events[-1]
        assert isinstance(completed, ProviderResponseCompleted)
        async for _event in provider.stream(
            messages,
            model="claude-test",
            tool_results=[ToolCallResult(call_id="call-id", output="ok")],
            previous_response_id=completed.response_id,
        ):
            pass

    anyio.run(run)

    replay_message = messages_resource.calls[1]["messages"][1]
    assert replay_message == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "call-id", "name": "lookup", "input": {}},
        ],
    }


def test_anthropic_provider_replay_preserves_thinking_blocks_alongside_tool_use() -> None:
    # Regression test: Anthropic's tool-use guidance requires thinking and
    # redacted_thinking blocks to be echoed back to the API unmodified
    # alongside their sibling tool_use block on the same turn -- dropping
    # them (e.g. replaying only text + tool_use) can be rejected or lose
    # reasoning continuity.
    messages_resource = StubMessagesResource(
        responses=[
            [
                _message_start("response-id"),
                _thinking_block_start(index=0),
                _thinking_delta("checking the lookup table", index=0),
                _signature_delta("sig-part-1", index=0),
                _signature_delta("sig-part-2", index=0),
                _redacted_thinking_block_start(index=1),
                _tool_use_start("call-id", "lookup", index=2),
                _input_json_delta("{}", index=2),
                _message_delta("tool_use"),
            ],
            [],
        ]
    )
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
    )
    messages = [WispMessage(role="user", content="hello")]

    async def run() -> None:
        first_events = [event async for event in provider.stream(messages, model="claude-test")]
        completed = first_events[-1]
        assert isinstance(completed, ProviderResponseCompleted)
        async for _event in provider.stream(
            messages,
            model="claude-test",
            tool_results=[ToolCallResult(call_id="call-id", output="ok")],
            previous_response_id=completed.response_id,
        ):
            pass

    anyio.run(run)

    replay_message = messages_resource.calls[1]["messages"][1]
    assert replay_message == {
        "role": "assistant",
        "content": [
            {
                "type": "thinking",
                "thinking": "checking the lookup table",
                "signature": "sig-part-1sig-part-2",
            },
            {"type": "redacted_thinking", "data": "redacted"},
            {"type": "tool_use", "id": "call-id", "name": "lookup", "input": {}},
        ],
    }


def test_anthropic_provider_accumulates_replay_across_multiple_tool_rounds() -> None:
    # Regression test: run_agent_loop never grows `messages` across tool
    # rounds and only ever passes each round's own new `tool_results` --
    # so round 2's replay must carry BOTH round 1's tool_use/tool_result
    # pair AND round 2's own tool_use turn, not just the latest round.
    # Losing round 1 here would silently drop an earlier tool observation
    # from a multi-step tool workflow.
    messages_resource = StubMessagesResource(
        responses=[
            [
                _message_start("response-1"),
                _tool_use_start("call-1", "lookup", index=0),
                _input_json_delta('{"query": "a"}', index=0),
                _message_delta("tool_use"),
            ],
            [
                _message_start("response-2"),
                _tool_use_start("call-2", "lookup", index=0),
                _input_json_delta('{"query": "b"}', index=0),
                _message_delta("tool_use"),
            ],
            [],
        ]
    )
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
    )
    messages = [WispMessage(role="user", content="hello")]

    async def run() -> None:
        # Round 1: no prior tool state.
        first_events = [event async for event in provider.stream(messages, model="claude-test")]
        first_completed = first_events[-1]
        assert isinstance(first_completed, ProviderResponseCompleted)

        # Round 2: mirrors run_agent_loop exactly -- same fixed `messages`,
        # only this round's own tool_results, previous_response_id carried
        # forward from round 1's response.
        second_events = [
            event
            async for event in provider.stream(
                messages,
                model="claude-test",
                tool_results=[ToolCallResult(call_id="call-1", output="result-a")],
                previous_response_id=first_completed.response_id,
            )
        ]
        second_completed = second_events[-1]
        assert isinstance(second_completed, ProviderResponseCompleted)

        # Round 3: same shape again, now carrying round 2's tool_results.
        async for _event in provider.stream(
            messages,
            model="claude-test",
            tool_results=[ToolCallResult(call_id="call-2", output="result-b")],
            previous_response_id=second_completed.response_id,
        ):
            pass

    anyio.run(run)

    assert len(messages_resource.calls) == 3
    third_call_messages = messages_resource.calls[2]["messages"]
    assert third_call_messages == [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {"query": "a"}}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "result-a",
                    "is_error": False,
                }
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call-2", "name": "lookup", "input": {"query": "b"}}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-2",
                    "content": "result-b",
                    "is_error": False,
                }
            ],
        },
    ]


def test_anthropic_provider_tool_results_without_a_replay_omit_the_assistant_turn() -> None:
    # No prior tool-use turn was ever streamed for this previous_response_id
    # (e.g. it predates this provider instance, or belonged to a different
    # provider) -- fall through gracefully rather than raising or fabricating
    # a turn, matching the "advisory, never blocking" spirit of the rest of
    # the registry-adjacent code in this codebase.
    messages_resource = StubMessagesResource(responses=[[]])
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
    )

    async def run() -> None:
        async for _event in provider.stream(
            [WispMessage(role="user", content="hello")],
            model="claude-test",
            tool_results=[ToolCallResult(call_id="call-id", output="ok")],
            previous_response_id="unknown-response-id",
        ):
            pass

    anyio.run(run)

    assert messages_resource.calls[0]["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-id",
                    "content": "ok",
                    "is_error": False,
                }
            ],
        },
    ]


def test_anthropic_provider_maps_context_window_exceeded_to_length() -> None:
    # "model_context_window_exceeded" is a beta-only stop reason (Anthropic's
    # server-side compaction surface, not the GA endpoint this provider
    # calls) included defensively in the explicit mapping table in case it
    # ever appears outside beta. It means the response was truncated, so it
    # must report "length", not a clean "stop".
    provider = StubAnthropicProvider(
        [_text_delta("partial answer"), _message_delta("model_context_window_exceeded")]
    )

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    events = anyio.run(run)
    completed = events[-1]
    assert isinstance(completed, ProviderResponseCompleted)
    assert completed.finish_reason == "length"


def test_anthropic_provider_defaults_unrecognized_stop_reason_to_length() -> None:
    # Regression test: a stop_reason this provider has never seen before
    # (not in _STOP_REASON_TO_FINISH_REASON at all -- distinct from the
    # explicitly-mapped-but-beta-only "model_context_window_exceeded" case
    # above) must never be silently reported as a clean completion via the
    # dict .get() fallback. "we don't recognize this" must default to
    # incomplete ("length"), not success ("stop").
    provider = StubAnthropicProvider(
        [_text_delta("partial answer"), _message_delta("some_future_stop_reason")]
    )

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    events = anyio.run(run)
    completed = events[-1]
    assert isinstance(completed, ProviderResponseCompleted)
    assert completed.finish_reason == "length"


class StubAsyncAnthropic:
    def __init__(self, messages: StubMessagesResource) -> None:
        self.messages = messages


class StubMessagesResource:
    def __init__(self, responses: Sequence[Sequence[RawMessageStreamEvent]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses) if responses is not None else None

    async def create(self, **kwargs: object) -> AsyncIterator[RawMessageStreamEvent]:
        self.calls.append(dict(kwargs))
        events = self._responses.pop(0) if self._responses else ()

        async def stream() -> AsyncIterator[RawMessageStreamEvent]:
            for event in events:
                yield event

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


def _signature_delta(signature: str, *, index: int) -> RawContentBlockDeltaEvent:
    return RawContentBlockDeltaEvent(
        delta=SignatureDelta(signature=signature, type="signature_delta"),
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


def _text_block_start(*, index: int = 0) -> RawContentBlockStartEvent:
    content_block = cast(ContentBlock, TextBlock(text="", type="text"))
    return RawContentBlockStartEvent(
        content_block=content_block,
        index=index,
        type="content_block_start",
    )


def _thinking_block_start(*, index: int = 0) -> RawContentBlockStartEvent:
    content_block = cast(ContentBlock, ThinkingBlock(thinking="", signature="", type="thinking"))
    return RawContentBlockStartEvent(
        content_block=content_block,
        index=index,
        type="content_block_start",
    )


def _redacted_thinking_block_start(*, index: int = 0) -> RawContentBlockStartEvent:
    content_block = cast(
        ContentBlock, RedactedThinkingBlock(data="redacted", type="redacted_thinking")
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
