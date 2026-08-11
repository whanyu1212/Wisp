from __future__ import annotations

from collections.abc import Sequence
from typing import cast

import anyio
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionChunk

from wisp.agent.messages import Message
from wisp.providers.base import ToolCallResult, ToolSpec
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderToolCallCompleted,
)
from wisp.providers.openai_compatible import OpenAICompatibleProvider


class _StubStream:
    def __init__(self, chunks: Sequence[ChatCompletionChunk]) -> None:
        self._chunks = iter(chunks)
        self.closed = False

    def __aiter__(self) -> _StubStream:
        return self

    async def __anext__(self) -> ChatCompletionChunk:
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration from None

    async def close(self) -> None:
        self.closed = True


class _StubCompletions:
    def __init__(self, responses: Sequence[Sequence[ChatCompletionChunk]]) -> None:
        self._responses = iter(responses)
        self.calls: list[dict[str, object]] = []
        self.streams: list[_StubStream] = []

    async def create(self, **kwargs: object) -> _StubStream:
        self.calls.append(kwargs)
        stream = _StubStream(next(self._responses))
        self.streams.append(stream)
        return stream


class _StubChat:
    def __init__(self, completions: _StubCompletions) -> None:
        self.completions = completions


class _StubClient:
    def __init__(self, completions: _StubCompletions) -> None:
        self.chat = _StubChat(completions)


def _chunk(
    *,
    content: str | None = None,
    finish_reason: str | None = None,
    tool_calls: list[dict[str, object]] | None = None,
    response_id: str = "chatcmpl-1",
    usage: dict[str, object] | None = None,
) -> ChatCompletionChunk:
    choices: list[dict[str, object]] = []
    if content is not None or finish_reason is not None or tool_calls is not None:
        delta: dict[str, object] = {}
        if content is not None:
            delta["content"] = content
        if tool_calls is not None:
            delta["tool_calls"] = tool_calls
        choices.append({"index": 0, "delta": delta, "finish_reason": finish_reason})
    return ChatCompletionChunk.model_validate(
        {
            "id": response_id,
            "choices": choices,
            "created": 0,
            "model": "test-model",
            "object": "chat.completion.chunk",
            "usage": usage,
        }
    )


def _provider(
    responses: Sequence[Sequence[ChatCompletionChunk]],
) -> tuple[OpenAICompatibleProvider, _StubCompletions]:
    completions = _StubCompletions(responses)
    client = cast(AsyncOpenAI, _StubClient(completions))
    return (
        OpenAICompatibleProvider(
            base_url="https://example.test/v1",
            default_model="test-model",
            client=client,
        ),
        completions,
    )


def _collect(provider: OpenAICompatibleProvider, **kwargs: object) -> list[object]:
    async def run() -> list[object]:
        return [
            event async for event in provider.stream([Message(role="user", content="hi")], **kwargs)
        ]

    return anyio.run(run)


def test_streams_text_and_usage_and_closes_stream() -> None:
    provider, completions = _provider(
        [
            [
                _chunk(content="hel"),
                _chunk(content="lo", finish_reason="stop"),
                _chunk(
                    usage={
                        "prompt_tokens": 3,
                        "completion_tokens": 2,
                        "total_tokens": 5,
                        "prompt_tokens_details": {"cached_tokens": 1},
                        "completion_tokens_details": {"reasoning_tokens": 1},
                    }
                ),
            ]
        ]
    )

    events = _collect(provider)

    assert isinstance(events[0], ProviderResponseStarted)
    assert [event.delta for event in events if isinstance(event, ProviderTextDelta)] == [
        "hel",
        "lo",
    ]
    completed = events[-1]
    assert isinstance(completed, ProviderResponseCompleted)
    assert completed.content == "hello"
    assert completed.response_id == "chatcmpl-1"
    assert completed.usage is not None
    assert completed.usage.total_tokens == 5
    assert completed.usage.cache_read_input_tokens == 1
    assert completed.usage.reasoning_output_tokens == 1
    assert completions.streams[0].closed is True


def test_serializes_tools_effort_and_fragmented_parallel_tool_calls() -> None:
    provider, completions = _provider(
        [
            [
                _chunk(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "look", "arguments": '{"q":'},
                        },
                        {
                            "index": 1,
                            "id": "call-2",
                            "type": "function",
                            "function": {"name": "read", "arguments": '{"p":'},
                        },
                    ]
                ),
                _chunk(
                    tool_calls=[
                        {"index": 0, "function": {"name": "up", "arguments": '"wisp"}'}},
                        {"index": 1, "function": {"arguments": '"README"}'}},
                    ],
                    finish_reason="tool_calls",
                ),
            ]
        ]
    )
    tool = ToolSpec(name="lookup", description="Find", input_schema={"type": "object"})

    events = _collect(provider, tools=(tool,), effort="high")

    streamed = [event.tool_call for event in events if isinstance(event, ProviderToolCallCompleted)]
    assert [(call.call_id, call.name, call.arguments) for call in streamed] == [
        ("call-1", "lookup", {"q": "wisp"}),
        ("call-2", "read", {"p": "README"}),
    ]
    completed = events[-1]
    assert isinstance(completed, ProviderResponseCompleted)
    assert completed.tool_calls == tuple(streamed)
    assert completed.finish_reason == "tool_calls"
    request = completions.calls[0]
    assert request["reasoning_effort"] == "high"
    assert request["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Find",
                "parameters": {"type": "object"},
            },
        }
    ]


def test_replays_assistant_tool_calls_and_tool_results_on_follow_up() -> None:
    provider, completions = _provider(
        [
            [
                _chunk(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ],
                    finish_reason="tool_calls",
                    response_id="first",
                )
            ],
            [_chunk(content="done", finish_reason="stop", response_id="second")],
        ]
    )

    first = _collect(provider)
    terminal = first[-1]
    assert isinstance(terminal, ProviderResponseCompleted)
    second = _collect(
        provider,
        previous_response_id=terminal.response_id,
        tool_results=(ToolCallResult(call_id="call-1", output="result"),),
    )

    assert isinstance(second[-1], ProviderResponseCompleted)
    messages = completions.calls[1]["messages"]
    assert messages == [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
    ]


def test_rejects_eof_without_finish_reason() -> None:
    provider, _ = _provider([[_chunk(content="partial")]])

    events = _collect(provider)

    assert isinstance(events[-1], ProviderResponseFailed)
    assert events[-1].partial_content == "partial"
    assert "finish reason" in events[-1].message


def test_reports_malformed_tool_arguments_without_executing_early() -> None:
    provider, _ = _provider(
        [
            [
                _chunk(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{"},
                        }
                    ]
                ),
                _chunk(finish_reason="tool_calls"),
            ]
        ]
    )

    events = _collect(provider)

    tool_event = next(event for event in events if isinstance(event, ProviderToolCallCompleted))
    assert tool_event.tool_call.arguments == {}
    assert tool_event.tool_call.parse_error is not None
