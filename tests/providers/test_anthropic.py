from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import anyio
import httpx
import pytest
from anthropic import APIConnectionError
from anthropic.types import RawMessageStreamEvent

from tests.providers.anthropic_support import (
    StubAnthropicProvider,
    input_json_delta_event,
    message_delta_event,
    message_start_event,
    text_delta_event,
    thinking_delta_event,
    tool_use_start_event,
)
from wisp.agent.messages import Message as WispMessage
from wisp.providers.anthropic import AnthropicProvider
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
    ProviderThinkingDelta,
    ProviderToolCallCompleted,
    ProviderUsage,
)

_EXPECTED_USAGE = ProviderUsage(
    input_tokens=1,
    output_tokens=1,
    total_tokens=2,
    context_input_tokens=1,
)


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
        effort: str | None = None,
    ) -> AsyncIterator[RawMessageStreamEvent]:
        async def stream() -> AsyncIterator[RawMessageStreamEvent]:
            yield text_delta_event("partial")
            raise APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))

        return stream()


def test_anthropic_provider_streams_text_deltas() -> None:
    provider = StubAnthropicProvider(
        [
            message_start_event("response-id"),
            text_delta_event("hello"),
            text_delta_event(" world", index=1),
            message_delta_event("end_turn"),
        ]
    )
    messages = [WispMessage(role="user", content="Say hello")]

    async def run() -> list[object]:
        return [event async for event in provider.stream(messages, model="claude-test")]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="claude-test"),
        ProviderTextDelta(delta="hello"),
        ProviderTextDelta(delta=" world", content_index=1),
        ProviderResponseCompleted(
            content="hello world",
            response_id="response-id",
            usage=_EXPECTED_USAGE,
        ),
    ]
    assert provider.seen_model == "claude-test"
    assert provider.seen_messages == messages


def test_anthropic_provider_uses_default_model_when_model_is_not_provided() -> None:
    provider = StubAnthropicProvider([text_delta_event("hello"), message_delta_event("end_turn")])

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
            thinking_delta_event("let me think", index=0),
            text_delta_event("the answer", index=1),
            message_delta_event("end_turn"),
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
            message_start_event("response-id"),
            tool_use_start_event("call-id", "lookup", index=0),
            input_json_delta_event('{"query": ', index=0),
            input_json_delta_event('"wisp"}', index=0),
            message_delta_event("tool_use"),
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
            usage=_EXPECTED_USAGE,
        ),
    ]
    assert provider.seen_tools == [tool]


def test_anthropic_provider_streams_tool_call_parse_errors() -> None:
    provider = StubAnthropicProvider(
        [
            message_start_event("response-id"),
            tool_use_start_event("call-id", "lookup", index=0),
            input_json_delta_event("not-json", index=0),
            message_delta_event("tool_use"),
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
            usage=_EXPECTED_USAGE,
        ),
    ]


def test_anthropic_provider_surfaces_truncated_tool_call_for_rejection() -> None:
    # Preserve the incomplete call beside finish_reason="length" so the
    # provider-neutral loop can return an in-band error without executing it.
    provider = StubAnthropicProvider(
        [
            message_start_event("response-id"),
            tool_use_start_event("call-id", "lookup", index=0),
            input_json_delta_event('{"query": "wi', index=0),
            message_delta_event("max_tokens"),
        ]
    )

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    events = anyio.run(run)

    assert isinstance(events[0], ProviderResponseStarted)
    tool_event = events[1]
    assert isinstance(tool_event, ProviderToolCallCompleted)
    assert tool_event.tool_call.call_id == "call-id"
    assert tool_event.tool_call.name == "lookup"
    assert tool_event.tool_call.raw_arguments == '{"query": "wi'
    assert tool_event.tool_call.parse_error is not None
    completed = events[-1]
    assert isinstance(completed, ProviderResponseCompleted)
    assert completed.tool_calls == (tool_event.tool_call,)
    assert completed.response_id == "response-id"
    assert completed.finish_reason == "length"
    assert completed.usage == _EXPECTED_USAGE


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
    provider = StubAnthropicProvider([text_delta_event("hi"), message_delta_event(stop_reason)])

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
    provider._replays.remember("previous-response", ())  # noqa: SLF001

    async def run() -> list[object]:
        return [
            event
            async for event in provider.stream(
                [WispMessage(role="user", content="hello")],
                previous_response_id="previous-response",
            )
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
    assert provider._replays.get("previous-response") is None  # noqa: SLF001


@pytest.mark.production_fault
def test_anthropic_provider_emits_failed_terminal_when_stream_ends_without_stop_reason() -> None:
    # Regression test: distinct from the exception-raising failure case above
    # -- here the async iterator ends *normally* (no exception), but a
    # dropped connection or truncated proxy response cut it off before any
    # message_delta ever carried a stop_reason. Silently falling through to
    # ProviderResponseCompleted(finish_reason="stop") would report a
    # truncated answer as a successful turn.
    provider = StubAnthropicProvider(
        [message_start_event("response-id"), text_delta_event("partial")]
    )

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


def test_anthropic_provider_reports_context_window_exceeded_as_failure() -> None:
    # This explicit stop reason must remain distinguishable from ordinary output
    # truncation so the agent loop can emit a structured context.overflow event.
    provider = StubAnthropicProvider(
        [text_delta_event("partial answer"), message_delta_event("model_context_window_exceeded")]
    )
    provider._replays.remember("previous-response", ())  # noqa: SLF001

    async def run() -> tuple[list[object], list[object]]:
        failed = [
            event
            async for event in provider.stream(
                [WispMessage(role="user", content="hello")],
                previous_response_id="previous-response",
            )
        ]
        provider.events = [
            message_start_event("recovered-response"),
            text_delta_event("recovered"),
            message_delta_event("end_turn"),
        ]
        recovered = [
            event
            async for event in provider.stream(
                [WispMessage(role="user", content="summary")],
                previous_response_id="previous-response",
            )
        ]
        return failed, recovered

    events, recovered = anyio.run(run)
    failure = events[-1]
    assert failure == ProviderResponseFailed(
        message="Anthropic model_context_window_exceeded",
        partial_content="partial answer",
    )
    assert isinstance(recovered[-1], ProviderResponseCompleted)
    assert recovered[-1].content == "recovered"


def test_anthropic_provider_defaults_unrecognized_stop_reason_to_length() -> None:
    # Regression test: a stop_reason this provider has never seen before
    # (not in _STOP_REASON_TO_FINISH_REASON at all -- distinct from the
    # explicitly-mapped-but-beta-only "model_context_window_exceeded" case
    # above) must never be silently reported as a clean completion via the
    # dict .get() fallback. "we don't recognize this" must default to
    # incomplete ("length"), not success ("stop").
    provider = StubAnthropicProvider(
        [text_delta_event("partial answer"), message_delta_event("some_future_stop_reason")]
    )

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    events = anyio.run(run)
    completed = events[-1]
    assert isinstance(completed, ProviderResponseCompleted)
    assert completed.finish_reason == "length"
