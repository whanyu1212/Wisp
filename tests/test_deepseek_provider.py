from __future__ import annotations

from collections.abc import Sequence
from typing import cast

import anyio
import pytest
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionChunk
from pytest import MonkeyPatch

from wisp.agent.messages import Message
from wisp.providers.base import ProviderConfigurationError, ToolCallResult, ToolSpec
from wisp.providers.deepseek import DEFAULT_DEEPSEEK_MODEL, DeepSeekProvider
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderTextDelta,
    ProviderThinkingDelta,
    ProviderToolCallCompleted,
)


class _StubStream:
    def __init__(self, chunks: Sequence[ChatCompletionChunk | Exception]) -> None:
        self._chunks = iter(chunks)
        self.closed = False

    def __aiter__(self) -> _StubStream:
        return self

    async def __anext__(self) -> ChatCompletionChunk:
        try:
            item = next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration from None
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


class _StubCompletions:
    def __init__(self, responses: Sequence[Sequence[ChatCompletionChunk | Exception]]) -> None:
        self._responses = iter(responses)
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> _StubStream:
        self.calls.append(kwargs)
        return _StubStream(next(self._responses))


class _StubChat:
    def __init__(self, completions: _StubCompletions) -> None:
        self.completions = completions


class _StubClient:
    def __init__(self, completions: _StubCompletions) -> None:
        self.chat = _StubChat(completions)


def _chunk(
    *,
    reasoning_content: str | None = None,
    content: str | None = None,
    finish_reason: str | None = None,
    tool_calls: list[dict[str, object]] | None = None,
    response_id: str = "chatcmpl-deepseek",
    usage: dict[str, object] | None = None,
) -> ChatCompletionChunk:
    choices: list[dict[str, object]] = []
    if any(value is not None for value in (reasoning_content, content, finish_reason, tool_calls)):
        delta: dict[str, object] = {}
        if reasoning_content is not None:
            delta["reasoning_content"] = reasoning_content
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
            "model": DEFAULT_DEEPSEEK_MODEL,
            "object": "chat.completion.chunk",
            "usage": usage,
        }
    )


def _provider(
    responses: Sequence[Sequence[ChatCompletionChunk | Exception]],
) -> tuple[DeepSeekProvider, _StubCompletions]:
    completions = _StubCompletions(responses)
    client = cast(AsyncOpenAI, _StubClient(completions))
    return DeepSeekProvider(client=client), completions


def _collect(provider: DeepSeekProvider, **kwargs: object) -> list[object]:
    async def run() -> list[object]:
        return [
            event async for event in provider.stream([Message(role="user", content="hi")], **kwargs)
        ]

    return anyio.run(run)


def test_streams_thinking_text_and_deepseek_usage() -> None:
    provider, _ = _provider(
        [
            [
                _chunk(reasoning_content="think "),
                _chunk(reasoning_content="carefully", content="answer", finish_reason="stop"),
                _chunk(
                    usage={
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                        "prompt_cache_hit_tokens": 7,
                        "prompt_cache_miss_tokens": 3,
                        "reasoning_tokens": 4,
                    }
                ),
            ]
        ]
    )

    events = _collect(provider)

    assert [event.delta for event in events if isinstance(event, ProviderThinkingDelta)] == [
        "think ",
        "carefully",
    ]
    assert [event.delta for event in events if isinstance(event, ProviderTextDelta)] == ["answer"]
    completed = events[-1]
    assert isinstance(completed, ProviderResponseCompleted)
    assert completed.usage is not None
    assert completed.usage.cache_read_input_tokens == 7
    assert completed.usage.reasoning_output_tokens == 4


def test_sends_thinking_effort_and_tools() -> None:
    provider, completions = _provider([[_chunk(content="done", finish_reason="stop")]])
    tool = ToolSpec(name="lookup", description="Find", input_schema={"type": "object"})

    _collect(provider, tools=(tool,), effort="max")

    request = completions.calls[0]
    assert request["reasoning_effort"] == "max"
    assert request["extra_body"] == {"thinking": {"type": "enabled"}}
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


def test_replays_reasoning_content_with_tool_call_and_result() -> None:
    provider, completions = _provider(
        [
            [
                _chunk(reasoning_content="I should inspect the repository."),
                _chunk(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "read", "arguments": '{"path":"README.md"}'},
                        }
                    ],
                    finish_reason="tool_calls",
                    response_id="first",
                ),
            ],
            [_chunk(reasoning_content="I have the result.", content="done", finish_reason="stop")],
        ]
    )

    first = _collect(provider)
    tool_event = next(event for event in first if isinstance(event, ProviderToolCallCompleted))
    terminal = first[-1]
    assert isinstance(terminal, ProviderResponseCompleted)

    second = _collect(
        provider,
        previous_response_id=terminal.response_id,
        tool_results=(ToolCallResult(call_id=tool_event.tool_call.call_id, output="contents"),),
    )

    assert isinstance(second[-1], ProviderResponseCompleted)
    assert completions.calls[1]["messages"] == [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"README.md"}'},
                }
            ],
            "reasoning_content": "I should inspect the repository.",
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "contents"},
    ]


def test_preserves_reasoning_across_multiple_tool_rounds() -> None:
    provider, completions = _provider(
        [
            [
                _chunk(reasoning_content="first thought"),
                _chunk(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "one", "arguments": "{}"},
                        }
                    ],
                    finish_reason="tool_calls",
                    response_id="first",
                ),
            ],
            [
                _chunk(reasoning_content="second thought"),
                _chunk(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call-2",
                            "type": "function",
                            "function": {"name": "two", "arguments": "{}"},
                        }
                    ],
                    finish_reason="tool_calls",
                    response_id="second",
                ),
            ],
            [_chunk(reasoning_content="final thought", content="done", finish_reason="stop")],
        ]
    )

    first = cast(ProviderResponseCompleted, _collect(provider)[-1])
    second = cast(
        ProviderResponseCompleted,
        _collect(
            provider,
            previous_response_id=first.response_id,
            tool_results=(ToolCallResult(call_id="call-1", output="one result"),),
        )[-1],
    )
    _collect(
        provider,
        previous_response_id=second.response_id,
        tool_results=(ToolCallResult(call_id="call-2", output="two result"),),
    )

    messages = cast(list[dict[str, object]], completions.calls[2]["messages"])
    assistant_rows = [message for message in messages if message["role"] == "assistant"]
    assert [message["reasoning_content"] for message in assistant_rows] == [
        "first thought",
        "second thought",
    ]


def test_reports_provider_specific_finish_reasons_as_failures() -> None:
    provider, _ = _provider([[_chunk(content="partial", finish_reason="content_filter")]])

    events = _collect(provider)

    assert isinstance(events[-1], ProviderResponseFailed)
    assert "content_filter" in events[-1].message
    assert events[-1].partial_content == "partial"


def test_missing_credentials_names_deepseek_environment(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    provider = DeepSeekProvider()

    async def run() -> None:
        with pytest.raises(
            ProviderConfigurationError,
            match="deepseek credentials are required.*`/connect deepseek`.*DEEPSEEK_API_KEY",
        ):
            await provider._client_or_create()  # noqa: SLF001

    anyio.run(run)


def test_thinking_mode_cannot_fresh_reconstruct_structured_tool_history() -> None:
    provider, _ = _provider([])

    assert provider.supports_structured_tool_replacement(effort=None) is False
    assert provider.supports_structured_tool_replacement(effort="high") is False
