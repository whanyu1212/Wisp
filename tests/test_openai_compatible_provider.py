from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import cast

import anyio
import pytest
from openai import AsyncOpenAI, DefaultAsyncHttpxClient, OpenAIError
from openai.types.chat import ChatCompletionChunk
from pytest import MonkeyPatch

from wisp.agent.messages import Message
from wisp.auth.storage import ApiKeyCredential, JsonAuthStore
from wisp.events import ToolCallSnapshot
from wisp.providers.auth import StoredProviderAuthResolver
from wisp.providers.base import (
    ProviderConfigurationError,
    ProviderProtocolError,
    ToolCallResult,
    ToolSpec,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderToolCallCompleted,
)
from wisp.providers.openai_compatible import OpenAICompatibleProvider


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
    responses: Sequence[Sequence[ChatCompletionChunk | Exception]],
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


def test_rejects_missing_continuation_state() -> None:
    provider, completions = _provider([])

    async def run() -> None:
        stream = provider.stream(
            [Message(role="user", content="hi")],
            previous_response_id="missing-response",
            extra_messages=[Message(role="user", content="steered")],
        )
        await anext(stream)

    with pytest.raises(
        ProviderProtocolError,
        match="openai-compatible continuation state is unavailable for missing-response",
    ):
        anyio.run(run)
    assert completions.calls == []


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


def test_preserves_continuation_after_context_overflow() -> None:
    provider, _ = _provider([[OpenAIError("maximum context length exceeded")]])
    provider._continuations.remember("previous-response", ())  # noqa: SLF001

    events = _collect(provider, previous_response_id="previous-response")

    assert isinstance(events[-1], ProviderResponseFailed)
    assert provider._continuations.get("previous-response") is not None  # noqa: SLF001


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
    assert "extra_body" not in request
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


def test_length_response_surfaces_accumulated_tool_fragments_for_rejection() -> None:
    provider, _ = _provider(
        [
            [
                _chunk(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"q":"wisp"}'},
                        }
                    ]
                ),
                _chunk(finish_reason="length"),
            ]
        ]
    )

    events = _collect(provider)

    tool_event = next(event for event in events if isinstance(event, ProviderToolCallCompleted))
    assert tool_event.tool_call.call_id == "call-1"
    assert tool_event.tool_call.name == "lookup"
    assert tool_event.tool_call.arguments == {"q": "wisp"}
    completed = events[-1]
    assert isinstance(completed, ProviderResponseCompleted)
    assert completed.finish_reason == "length"
    assert completed.tool_calls == (tool_event.tool_call,)


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


def test_replays_clean_response_before_appended_user_message() -> None:
    provider, completions = _provider(
        [
            [_chunk(content="first", finish_reason="stop", response_id="first")],
            [_chunk(content="second", finish_reason="stop", response_id="second")],
        ]
    )

    first = _collect(provider)
    terminal = first[-1]
    assert isinstance(terminal, ProviderResponseCompleted)
    second = _collect(
        provider,
        previous_response_id=terminal.response_id,
        extra_messages=(Message(role="user", content="steered"),),
    )

    assert isinstance(second[-1], ProviderResponseCompleted)
    assert completions.calls[1]["messages"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "first"},
        {"role": "user", "content": "steered"},
    ]


def test_fresh_context_appends_extra_messages() -> None:
    provider, completions = _provider([[_chunk(content="done", finish_reason="stop")]])

    _collect(
        provider,
        extra_messages=(Message(role="user", content="steered"),),
    )

    assert completions.calls[0]["messages"] == [
        {"role": "user", "content": "hi"},
        {"role": "user", "content": "steered"},
    ]


def test_replayed_history_tool_call_keeps_structured_tool_calls_and_paired_result() -> None:
    """A pre-existing assistant ``tool_calls`` message in ``messages`` (not the live
    in-flight round) must round-trip onto the wire as structured tool_calls with its
    result paired as a native ``role: tool`` message.

    ``_messages_to_chat`` currently drops ``tool_calls`` on every history message and
    flattens tool-role rows into ``{"role": "tool", "content": ...}`` without a
    ``tool_call_id`` on the assistant side — so a provider replaying an
    already-in-transcript tool round (as happens when Wisp rebuilds the harness
    transcript after mid-turn compaction) loses the model's record of having made the
    call. This asserts the wire payload actually preserves it end to end.
    """

    provider, completions = _provider([[_chunk(content="done", finish_reason="stop")]])

    history = [
        Message(role="user", content="search"),
        Message(
            role="assistant",
            content="checking",
            tool_calls=(
                ToolCallSnapshot(call_id="call-1", name="lookup", arguments={"query": "wisp"}),
            ),
        ),
        Message(role="tool", content="found it", tool_name="lookup", tool_call_id="call-1"),
    ]

    async def run() -> list[object]:
        return [event async for event in provider.stream(history)]

    anyio.run(run)

    sent = completions.calls[0]["messages"]
    assistant_sent = sent[1]
    assert assistant_sent.get("tool_calls") == [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "lookup", "arguments": '{"query": "wisp"}'},
        }
    ]
    tool_sent = sent[2]
    assert tool_sent == {"role": "tool", "tool_call_id": "call-1", "content": "found it"}


def test_normalized_historical_tool_call_never_reaches_wire_dangling() -> None:
    """A historical (pre-active-turn) assistant tool-call row with nonblank content
    must not reach the wire with structured ``tool_calls`` once its paired result
    has been narrated into user-role text — that combination is a live function
    call with no matching function output, which strict endpoints reject.

    ``normalize_provider_history`` is the layer responsible for stripping
    ``tool_calls`` from historical rows; this integration test drives its output
    through the real provider serializer to confirm the malformed shape can never
    reach the wire, rather than only asserting on the normalizer's return value.
    """

    from wisp.agent.messages import normalize_provider_history

    transcript = [
        Message(role="user", content="search"),
        Message(
            role="assistant",
            content="I'll check that",
            tool_calls=(
                ToolCallSnapshot(call_id="call-1", name="lookup", arguments={"query": "wisp"}),
            ),
        ),
        Message(role="tool", content="found it", tool_name="lookup", tool_call_id="call-1"),
        Message(role="user", content="what next?"),
    ]
    # active_from=3: only the final user row is still in progress. The
    # assistant/tool pair above it is historical and must be fully narrated.
    normalized = normalize_provider_history(transcript, active_from=3)

    provider, completions = _provider([[_chunk(content="done", finish_reason="stop")]])

    async def run() -> list[object]:
        return [event async for event in provider.stream(normalized)]

    anyio.run(run)

    sent = completions.calls[0]["messages"]
    assert all(message.get("tool_calls") is None for message in sent)


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


def test_custom_provider_api_key_precedence_and_stored_lookup(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    store = JsonAuthStore(tmp_path / "auth.json")
    store.set("openrouter", ApiKeyCredential(key="stored-key"))
    resolver = StoredProviderAuthResolver(store)
    monkeypatch.setenv("OPENROUTER_API_KEY", "provider-env-key")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "fallback-env-key")

    def provider(*, api_key: str | None = None) -> OpenAICompatibleProvider:
        return OpenAICompatibleProvider(
            provider_name="openrouter",
            base_url="https://openrouter.ai/api/v1",
            default_model="test-model",
            api_key=api_key,
            auth_resolver=resolver,
        )

    async def run() -> None:
        explicit = provider(api_key="explicit-key")
        provider_env = provider()
        explicit_client = await explicit._client_or_create()  # noqa: SLF001
        provider_env_client = await provider_env._client_or_create()  # noqa: SLF001

        monkeypatch.delenv("OPENROUTER_API_KEY")
        fallback_env = provider()
        fallback_env_client = await fallback_env._client_or_create()  # noqa: SLF001

        monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY")
        stored = provider()
        stored_client = await stored._client_or_create()  # noqa: SLF001

        assert explicit_client.api_key == "explicit-key"
        assert provider_env_client.api_key == "provider-env-key"
        assert fallback_env_client.api_key == "fallback-env-key"
        assert stored_client.api_key == "stored-key"

        await explicit.aclose()
        await provider_env.aclose()
        await fallback_env.aclose()
        await stored.aclose()

    anyio.run(run)


def test_custom_provider_error_uses_provider_name_and_environment_hint(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    provider = OpenAICompatibleProvider(
        provider_name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        default_model="test-model",
    )

    async def run() -> None:
        with pytest.raises(
            ProviderConfigurationError,
            match=(
                "openrouter credentials are required.*`/connect openrouter`.*"
                "OPENROUTER_API_KEY.*OPENAI_COMPATIBLE_API_KEY"
            ),
        ):
            await provider._client_or_create()  # noqa: SLF001

    anyio.run(run)


def test_custom_ca_bundle_configures_http_transport(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    ca_bundle = tmp_path / "private-ca.pem"
    ca_bundle.write_text("test CA", encoding="utf-8")
    captured: dict[str, object] = {}

    class RecordingDefaultAsyncHttpxClient(DefaultAsyncHttpxClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured.update(kwargs)
            kwargs.pop("verify", None)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(
        "wisp.providers.openai_compatible.DefaultAsyncHttpxClient",
        RecordingDefaultAsyncHttpxClient,
    )
    provider = OpenAICompatibleProvider(
        provider_name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        default_model="test-model",
        ca_bundle=ca_bundle,
        api_key="test-key",
    )

    async def run() -> None:
        client = await provider._client_or_create()  # noqa: SLF001
        assert captured["verify"] == str(ca_bundle)
        assert client._client.follow_redirects is True  # noqa: SLF001
        await provider.aclose()

    anyio.run(run)
