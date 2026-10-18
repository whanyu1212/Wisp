from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import anyio
import httpx
import pytest
from google.genai import types as genai_types

from tests.providers.google_support import (
    StubGoogleProvider,
    part_chunk,
    parts_chunk,
    text_chunk,
)
from wisp.messages import Message as WispMessage
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
from wisp.providers.google import GoogleProvider


class FailingGoogleProvider(GoogleProvider):
    def __init__(self, message: str = "boom") -> None:
        super().__init__(api_key="test-key", default_model="default-test-model")
        self.message = message

    async def _create_stream(
        self,
        messages: Sequence[WispMessage],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[genai_types.GenerateContentResponse]:
        async def stream() -> AsyncIterator[genai_types.GenerateContentResponse]:
            yield text_chunk("partial")
            raise httpx.ConnectError(self.message)

        return stream()


def test_google_provider_streams_text_deltas() -> None:
    provider = StubGoogleProvider(
        [
            text_chunk("hello", response_id="response-id"),
            text_chunk(" world", finish_reason=genai_types.FinishReason.STOP),
        ]
    )
    messages = [WispMessage(role="user", content="Say hello")]

    async def run() -> list[object]:
        return [event async for event in provider.stream(messages, model="gemini-test")]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="gemini-test"),
        ProviderTextDelta(delta="hello"),
        ProviderTextDelta(delta=" world", content_index=1),
        ProviderResponseCompleted(content="hello world", response_id="response-id"),
    ]
    assert provider.seen_model == "gemini-test"
    assert provider.seen_messages == messages


def test_google_provider_uses_default_model_when_model_is_not_provided() -> None:
    provider = StubGoogleProvider(
        [text_chunk("hello", finish_reason=genai_types.FinishReason.STOP)]
    )

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


def test_google_provider_streams_thinking_deltas() -> None:
    provider = StubGoogleProvider(
        [
            part_chunk(genai_types.Part(text="let me think", thought=True)),
            text_chunk("the answer", finish_reason=genai_types.FinishReason.STOP),
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


def test_google_provider_preserves_last_non_empty_usage_metadata() -> None:
    usage_chunk = text_chunk("answer").model_copy(
        update={
            "usage_metadata": genai_types.GenerateContentResponseUsageMetadata(
                prompt_token_count=12,
                candidates_token_count=None,
                thoughts_token_count=3,
                tool_use_prompt_token_count=5,
                total_token_count=20,
            )
        }
    )
    terminal_chunk = text_chunk("", finish_reason=genai_types.FinishReason.STOP).model_copy(
        update={"usage_metadata": genai_types.GenerateContentResponseUsageMetadata()}
    )
    provider = StubGoogleProvider([usage_chunk, terminal_chunk])

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderTextDelta(delta="answer"),
        ProviderResponseCompleted(
            content="answer",
            usage=ProviderUsage(
                input_tokens=17,
                output_tokens=0,
                total_tokens=20,
                reasoning_output_tokens=3,
            ),
        ),
    ]


def test_google_provider_streams_tool_calls() -> None:
    provider = StubGoogleProvider(
        [
            part_chunk(
                genai_types.Part(
                    function_call=genai_types.FunctionCall(
                        id="call-id", name="lookup", args={"query": "wisp"}
                    )
                ),
                response_id="response-id",
                finish_reason=genai_types.FinishReason.STOP,
            )
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


def test_google_provider_generates_unique_fallback_ids_for_parallel_calls_without_an_id() -> None:
    # Regression test: confirmed live against gemini-2.5-flash that a
    # parallel response with two calls to the same tool both return
    # function_call.id == None. Falling back to a name-only id would
    # collapse both calls to the same ToolCall.call_id, corrupting
    # parallel tool execution and functionResponse matching on replay.
    provider = StubGoogleProvider(
        [
            parts_chunk(
                [
                    genai_types.Part(
                        function_call=genai_types.FunctionCall(
                            name="lookup", args={"query": "wisp"}
                        )
                    ),
                    genai_types.Part(
                        function_call=genai_types.FunctionCall(name="lookup", args={"query": "fog"})
                    ),
                ],
                response_id="response-id",
                finish_reason=genai_types.FinishReason.STOP,
            )
        ]
    )

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    events = anyio.run(run)
    completed = events[-1]
    assert isinstance(completed, ProviderResponseCompleted)
    call_ids = [call.call_id for call in completed.tool_calls]
    assert len(call_ids) == 2
    assert len(set(call_ids)) == 2


def test_google_provider_surfaces_truncated_tool_call_for_rejection() -> None:
    # Preserve the observed call beside finish_reason="length" so the
    # provider-neutral loop can return an in-band error without executing it.
    provider = StubGoogleProvider(
        [
            part_chunk(
                genai_types.Part(
                    function_call=genai_types.FunctionCall(
                        id="call-id", name="lookup", args={"query": "wisp"}
                    )
                ),
                response_id="response-id",
                finish_reason=genai_types.FinishReason.MAX_TOKENS,
            )
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
    assert tool_event.tool_call.arguments == {"query": "wisp"}
    completed = events[-1]
    assert isinstance(completed, ProviderResponseCompleted)
    assert completed.tool_calls == (tool_event.tool_call,)
    assert completed.response_id == "response-id"
    assert completed.finish_reason == "length"


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [
        (genai_types.FinishReason.STOP, "stop"),
        (genai_types.FinishReason.MAX_TOKENS, "length"),
        (genai_types.FinishReason.SAFETY, "length"),
        (genai_types.FinishReason.RECITATION, "length"),
    ],
)
def test_google_provider_maps_finish_reasons(
    finish_reason: genai_types.FinishReason, expected: str
) -> None:
    provider = StubGoogleProvider([text_chunk("hi", finish_reason=finish_reason)])

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    events = anyio.run(run)
    completed = events[-1]
    assert isinstance(completed, ProviderResponseCompleted)
    assert completed.finish_reason == expected


def test_google_provider_emits_failed_terminal_on_stream_error() -> None:
    provider = FailingGoogleProvider()
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
    assert isinstance(events[2], ProviderResponseFailed)
    assert events[2].partial_content == "partial"
    assert provider._replays.get("previous-response") is None  # noqa: SLF001


def test_google_provider_preserves_replay_after_context_overflow() -> None:
    provider = FailingGoogleProvider("maximum context length exceeded")
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

    assert isinstance(events[-1], ProviderResponseFailed)
    assert provider._replays.get("previous-response") == ()  # noqa: SLF001


@pytest.mark.production_fault
def test_google_provider_emits_failed_terminal_when_stream_ends_without_finish_reason() -> None:
    # Regression guard, mirroring AnthropicProvider's equivalent: the stream
    # ended (cleanly or not) without Gemini ever reporting a finish_reason.
    # Silently completing here would report a truncated answer as success.
    provider = StubGoogleProvider([text_chunk("partial", response_id="response-id")])

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    events = anyio.run(run)
    assert isinstance(events[-1], ProviderResponseFailed)
    assert events[-1].partial_content == "partial"
