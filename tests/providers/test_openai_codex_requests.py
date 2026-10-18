from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import anyio
import h11
import httpx
import pytest

from tests.providers.openai_codex_support import (
    StubOpenAICodexProvider,
    completed_event,
    store_with_oauth,
)
from wisp.messages import Message
from wisp.providers import openai_codex as openai_codex_module
from wisp.providers.auth import StoredProviderAuthResolver
from wisp.providers.base import (
    ProviderProtocolError,
    ToolCallResult,
    ToolSpec,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
)


def test_openai_codex_provider_sends_reasoning_effort_when_provided(tmp_path: Path) -> None:
    store = store_with_oauth(tmp_path)
    provider = StubOpenAICodexProvider(
        [
            {"type": "response.created", "response": {"id": "response-id"}},
            completed_event("response-id"),
        ],
        auth_resolver=StoredProviderAuthResolver(store),
    )

    async def run() -> list[object]:
        return [
            event
            async for event in provider.stream([Message(role="user", content="hi")], effort="high")
        ]

    anyio.run(run)

    assert provider.seen_body is not None
    assert provider.seen_body["reasoning"] == {"effort": "high"}


def test_openai_codex_provider_omits_reasoning_when_effort_is_not_provided(
    tmp_path: Path,
) -> None:
    store = store_with_oauth(tmp_path)
    provider = StubOpenAICodexProvider(
        [
            {"type": "response.created", "response": {"id": "response-id"}},
            completed_event("response-id"),
        ],
        auth_resolver=StoredProviderAuthResolver(store),
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hi")])]

    anyio.run(run)

    assert provider.seen_body is not None
    assert "reasoning" not in provider.seen_body


def test_openai_codex_provider_sends_prompt_cache_key_when_provided(tmp_path: Path) -> None:
    store = store_with_oauth(tmp_path)
    provider = StubOpenAICodexProvider(
        [
            {"type": "response.created", "response": {"id": "response-id"}},
            completed_event("response-id"),
        ],
        auth_resolver=StoredProviderAuthResolver(store),
    )

    async def run() -> list[object]:
        return [
            event
            async for event in provider.stream(
                [Message(role="user", content="hi")],
                prompt_cache_key="wisp:session-1",
            )
        ]

    anyio.run(run)

    assert provider.seen_body is not None
    assert provider.seen_body["prompt_cache_key"] == "wisp:session-1"
    # The backend only reuses cached prefixes for requests carrying this header.
    assert provider.seen_headers is not None
    assert provider.seen_headers["session_id"] == "wisp:session-1"


@pytest.mark.parametrize(
    "prompt_cache_key",
    ["wisp:sessión-1", "wisp:a\r\nX-Injected: 1", " wisp:key", "wisp:key ", "wisp:a\tb", ""],
)
def test_openai_codex_provider_sends_header_safe_session_id_for_any_cache_key(
    tmp_path: Path, prompt_cache_key: str
) -> None:
    store = store_with_oauth(tmp_path)
    provider = StubOpenAICodexProvider(
        [
            {"type": "response.created", "response": {"id": "response-id"}},
            completed_event("response-id"),
        ],
        auth_resolver=StoredProviderAuthResolver(store),
    )

    async def run() -> list[object]:
        return [
            event
            async for event in provider.stream(
                [Message(role="user", content="hi")],
                prompt_cache_key=prompt_cache_key,
            )
        ]

    anyio.run(run)

    assert provider.seen_body is not None
    assert provider.seen_body["prompt_cache_key"] == prompt_cache_key
    assert provider.seen_headers is not None
    session_id = provider.seen_headers["session_id"]
    assert session_id.startswith("sha256:")
    # The header must survive HTTP/1.1 serialization, which is stricter than
    # building an httpx.Request, with no extra header smuggled in.
    wire = _serialize_http11_request(provider.seen_headers)
    assert f"session_id: {session_id}\r\n".encode() in wire
    assert b"X-Injected" not in wire


def test_openai_codex_provider_keeps_wisp_cache_keys_as_the_session_id() -> None:
    key = "wisp:b7e168e544dc40de906662722af8bcd1"

    headers = openai_codex_module._codex_headers(token="t", account_id="a", session_id=key)

    assert headers["session_id"] == key
    assert f"session_id: {key}\r\n".encode() in _serialize_http11_request(headers)


def _serialize_http11_request(headers: Mapping[str, str]) -> bytes:
    """Serialize headers the way httpx's HTTP/1.1 transport does, via h11."""

    request = httpx.Request("POST", "https://example.test/codex/responses", headers=headers)
    connection = h11.Connection(h11.CLIENT)
    data = connection.send(
        h11.Request(
            method=request.method,
            target=request.url.raw_path,
            headers=[*request.headers.raw, (b"Content-Length", b"0")],
        )
    )
    assert data is not None
    return data


def test_openai_codex_provider_omits_prompt_cache_key_when_not_provided(
    tmp_path: Path,
) -> None:
    store = store_with_oauth(tmp_path)
    provider = StubOpenAICodexProvider(
        [
            {"type": "response.created", "response": {"id": "response-id"}},
            completed_event("response-id"),
        ],
        auth_resolver=StoredProviderAuthResolver(store),
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hi")])]

    anyio.run(run)

    assert provider.seen_body is not None
    assert "prompt_cache_key" not in provider.seen_body
    assert provider.seen_headers is not None
    assert "session_id" not in provider.seen_headers


def test_openai_codex_provider_appends_user_messages_in_fresh_request(tmp_path: Path) -> None:
    provider = StubOpenAICodexProvider(
        [completed_event("response-id")],
        auth_resolver=StoredProviderAuthResolver(store_with_oauth(tmp_path)),
    )

    async def run() -> None:
        async for _event in provider.stream(
            [Message(role="user", content="hi")],
            extra_messages=[Message(role="user", content="steered")],
        ):
            pass

    anyio.run(run)

    assert provider.seen_body is not None
    assert provider.seen_body["input"] == [
        {"role": "user", "content": "hi"},
        {"role": "user", "content": "steered"},
    ]


def test_openai_codex_provider_serializes_tools_and_tool_results(tmp_path: Path) -> None:
    store = store_with_oauth(tmp_path)
    provider = StubOpenAICodexProvider(
        [
            {"type": "response.created", "response": {"id": "response-id"}},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "reasoning",
                    "id": "reasoning-id",
                    "encrypted_content": "encrypted",
                },
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "item-id",
                    "call_id": "call-id",
                    "name": "lookup",
                    "arguments": '{"query":"wisp"}',
                },
            },
            completed_event("response-id"),
        ],
        auth_resolver=StoredProviderAuthResolver(store),
    )
    tool = ToolSpec(
        name="lookup",
        description="Look something up.",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
    )
    result = ToolCallResult(call_id="call-id", output="found")

    async def run() -> None:
        first_events = [
            event
            async for event in provider.stream(
                [Message(role="user", content="hi")],
                tools=[tool],
            )
        ]
        assert isinstance(first_events[-1], ProviderResponseCompleted)
        assert first_events[-1].response_id == "response-id"
        provider.events = [completed_event("response-id")]
        assert [
            event
            async for event in provider.stream(
                [Message(role="user", content="hi")],
                tools=[tool],
                tool_results=[result],
                extra_messages=[Message(role="user", content="steered")],
                previous_response_id="response-id",
            )
        ] == [
            ProviderResponseStarted(model="gpt-test"),
            ProviderResponseCompleted(content="", response_id="response-id"),
        ]

    anyio.run(run)

    assert provider.seen_body is not None
    assert provider.seen_body["input"] == [
        {"role": "user", "content": "hi"},
        {
            "type": "reasoning",
            "encrypted_content": "encrypted",
        },
        {
            "type": "function_call",
            "call_id": "call-id",
            "name": "lookup",
            "arguments": '{"query":"wisp"}',
        },
        {"type": "function_call_output", "call_id": "call-id", "output": "found"},
        {"role": "user", "content": "steered"},
    ]
    assert "previous_response_id" not in provider.seen_body
    assert provider.seen_body["tools"] == [
        {
            "type": "function",
            "name": "lookup",
            "description": "Look something up.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
            "strict": False,
        }
    ]


def test_openai_codex_provider_rejects_missing_continuation_state(tmp_path: Path) -> None:
    provider = StubOpenAICodexProvider(
        [],
        auth_resolver=StoredProviderAuthResolver(store_with_oauth(tmp_path)),
    )

    async def run() -> None:
        with pytest.raises(
            ProviderProtocolError,
            match="continuation state is unavailable for missing-response",
        ):
            async for _event in provider.stream(
                [Message(role="user", content="hi")],
                tool_results=[ToolCallResult(call_id="call-id", output="found")],
                previous_response_id="missing-response",
            ):
                pass

    anyio.run(run)
    assert provider.seen_body is None
