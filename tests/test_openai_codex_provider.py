from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

from wisp.agent.messages import Message
from wisp.auth.storage import JsonAuthStore, OAuthCredential
from wisp.providers.auth import StoredProviderAuthResolver
from wisp.providers.base import (
    ProviderConfigurationError,
    ProviderError,
    ProviderProtocolError,
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
    ProviderToolCallCompleted,
)
from wisp.providers.openai_codex import OpenAICodexProvider
from wisp.retry import RetryPolicy


class StubOpenAICodexProvider(OpenAICodexProvider):
    def __init__(
        self,
        events: Sequence[dict[str, object]],
        *,
        auth_resolver: StoredProviderAuthResolver,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        super().__init__(
            auth_resolver=auth_resolver,
            default_model="gpt-test",
            retry_policy=retry_policy,
        )
        self.events = events
        self.opening_errors: list[Exception] = []
        self.seen_body: Mapping[str, object] | None = None
        self.seen_bodies: list[Mapping[str, object]] = []
        self.seen_headers: Mapping[str, str] | None = None

    @asynccontextmanager
    async def _create_stream(
        self,
        *,
        body: Mapping[str, object],
        headers: Mapping[str, str],
    ) -> AsyncIterator[AsyncIterator[dict[str, object]]]:
        self.seen_body = body
        self.seen_bodies.append(body)
        self.seen_headers = headers
        if self.opening_errors:
            raise self.opening_errors.pop(0)

        async def stream() -> AsyncIterator[dict[str, object]]:
            for event in self.events:
                yield event

        yield stream()


def test_openai_codex_provider_streams_text_with_subscription_headers(tmp_path: Path) -> None:
    store = _store_with_oauth(tmp_path)
    provider = StubOpenAICodexProvider(
        [
            {"type": "response.created", "response": {"id": "response-id"}},
            {"type": "response.output_text.delta", "delta": "hello"},
            {"type": "response.output_text.delta", "delta": " world"},
        ],
        auth_resolver=StoredProviderAuthResolver(store),
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hi")])]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="gpt-test"),
        ProviderTextDelta(delta="hello"),
        ProviderTextDelta(delta=" world"),
        ProviderResponseCompleted(content="hello world", response_id="response-id"),
    ]
    assert provider.seen_body is not None
    assert provider.seen_body["model"] == "gpt-test"
    assert provider.seen_body["input"] == [{"role": "user", "content": "hi"}]
    assert provider.seen_headers is not None
    assert provider.seen_headers["Authorization"] == f"Bearer {_fake_codex_token()}"
    assert provider.seen_headers["chatgpt-account-id"] == "account-id"
    assert provider.seen_headers["OpenAI-Beta"] == "responses=experimental"


def test_openai_codex_provider_sends_reasoning_effort_when_provided(tmp_path: Path) -> None:
    store = _store_with_oauth(tmp_path)
    provider = StubOpenAICodexProvider(
        [{"type": "response.created", "response": {"id": "response-id"}}],
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
    store = _store_with_oauth(tmp_path)
    provider = StubOpenAICodexProvider(
        [{"type": "response.created", "response": {"id": "response-id"}}],
        auth_resolver=StoredProviderAuthResolver(store),
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hi")])]

    anyio.run(run)

    assert provider.seen_body is not None
    assert "reasoning" not in provider.seen_body


def test_openai_codex_provider_serializes_tools_and_tool_results(tmp_path: Path) -> None:
    store = _store_with_oauth(tmp_path)
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
        provider.events = []
        assert [
            event
            async for event in provider.stream(
                [Message(role="user", content="hi")],
                tools=[tool],
                tool_results=[result],
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
        auth_resolver=StoredProviderAuthResolver(_store_with_oauth(tmp_path)),
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


def test_openai_codex_provider_preserves_continuation_after_opening_failure(
    tmp_path: Path,
) -> None:
    provider = StubOpenAICodexProvider(
        [
            {"type": "response.created", "response": {"id": "response-1"}},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "item-1",
                    "call_id": "call-1",
                    "name": "lookup",
                    "arguments": '{"query":"wisp"}',
                },
            },
        ],
        auth_resolver=StoredProviderAuthResolver(_store_with_oauth(tmp_path)),
        retry_policy=RetryPolicy(max_retries=0),
    )
    messages = [Message(role="user", content="hi")]
    tool_result = ToolCallResult(call_id="call-1", output="found")

    async def run() -> None:
        async for _event in provider.stream(messages):
            pass

        provider.opening_errors.append(httpx.ConnectError("temporary failure"))
        with pytest.raises(httpx.ConnectError, match="temporary failure"):
            async for _event in provider.stream(
                messages,
                tool_results=[tool_result],
                previous_response_id="response-1",
            ):
                pass

        provider.events = [
            {"type": "response.created", "response": {"id": "response-2"}},
            {"type": "response.output_text.delta", "delta": "recovered"},
        ]
        assert [
            event
            async for event in provider.stream(
                messages,
                tool_results=[tool_result],
                previous_response_id="response-1",
            )
        ] == [
            ProviderResponseStarted(model="gpt-test"),
            ProviderTextDelta(delta="recovered"),
            ProviderResponseCompleted(content="recovered", response_id="response-2"),
        ]

        with pytest.raises(
            ProviderProtocolError,
            match="continuation state is unavailable for response-1",
        ):
            async for _event in provider.stream(
                messages,
                tool_results=[tool_result],
                previous_response_id="response-1",
            ):
                pass

    anyio.run(run)

    failed_body, retry_body = provider.seen_bodies[-2:]
    assert failed_body == retry_body
    assert "previous_response_id" not in retry_body


def test_openai_codex_provider_yields_tool_calls(tmp_path: Path) -> None:
    store = _store_with_oauth(tmp_path)
    provider = StubOpenAICodexProvider(
        [
            {"type": "response.created", "response": {"id": "response-id"}},
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
        ],
        auth_resolver=StoredProviderAuthResolver(store),
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hi")])]

    events = anyio.run(run)
    tool_call = ToolCall(
        call_id="call-id",
        name="lookup",
        arguments={"query": "wisp"},
        raw_arguments='{"query":"wisp"}',
        response_id="response-id",
    )
    assert events == [
        ProviderResponseStarted(model="gpt-test"),
        ProviderToolCallCompleted(tool_call=tool_call),
        ProviderResponseCompleted(
            content="",
            tool_calls=(tool_call,),
            response_id="response-id",
            finish_reason="tool_calls",
        ),
    ]


def test_openai_codex_provider_replays_multi_round_tool_history(tmp_path: Path) -> None:
    provider = StubOpenAICodexProvider(
        [
            {"type": "response.created", "response": {"id": "response-1"}},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "reasoning",
                    "id": "reasoning-1",
                    "encrypted_content": "encrypted-1",
                },
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "item-1",
                    "call_id": "call-1",
                    "name": "lookup",
                    "arguments": '{"query":"first"}',
                },
            },
        ],
        auth_resolver=StoredProviderAuthResolver(_store_with_oauth(tmp_path)),
    )
    messages = [Message(role="user", content="hi")]

    async def run() -> None:
        async for _event in provider.stream(messages):
            pass

        provider.events = [
            {"type": "response.created", "response": {"id": "response-2"}},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "reasoning",
                    "id": "reasoning-2",
                    "encrypted_content": "encrypted-2",
                },
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "item-2",
                    "call_id": "call-2",
                    "name": "lookup",
                    "arguments": '{"query":"second"}',
                },
            },
        ]
        async for _event in provider.stream(
            messages,
            tool_results=[ToolCallResult(call_id="call-1", output="first result")],
            previous_response_id="response-1",
        ):
            pass

        provider.events = [
            {"type": "response.created", "response": {"id": "response-3"}},
            {"type": "response.output_text.delta", "delta": "done"},
        ]
        async for _event in provider.stream(
            messages,
            tool_results=[ToolCallResult(call_id="call-2", output="second result")],
            previous_response_id="response-2",
        ):
            pass

    anyio.run(run)

    assert provider.seen_bodies[-1]["input"] == [
        {"role": "user", "content": "hi"},
        {"type": "reasoning", "encrypted_content": "encrypted-1"},
        {
            "type": "function_call",
            "call_id": "call-1",
            "name": "lookup",
            "arguments": '{"query":"first"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": "first result",
        },
        {"type": "reasoning", "encrypted_content": "encrypted-2"},
        {
            "type": "function_call",
            "call_id": "call-2",
            "name": "lookup",
            "arguments": '{"query":"second"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call-2",
            "output": "second result",
        },
    ]
    assert all("previous_response_id" not in body for body in provider.seen_bodies)


def test_openai_codex_provider_requires_login(tmp_path: Path) -> None:
    provider = OpenAICodexProvider(
        auth_resolver=StoredProviderAuthResolver(JsonAuthStore(tmp_path / "auth.json"))
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hi")])]

    with pytest.raises(ProviderConfigurationError, match="wisp auth login openai-codex"):
        anyio.run(run)


def test_openai_codex_provider_does_not_start_before_http_success(tmp_path: Path) -> None:
    store = _store_with_oauth(tmp_path)

    async def run() -> list[object]:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(401, text="invalid credentials")
        )
        async with httpx.AsyncClient(transport=transport) as client:
            provider = OpenAICodexProvider(
                auth_resolver=StoredProviderAuthResolver(store),
                client=client,
            )
            events: list[object] = []
            with pytest.raises(ProviderError, match=r"OpenAI Codex API error \(401\)"):
                async for event in provider.stream([Message(role="user", content="hi")]):
                    events.append(event)
            return events

    assert anyio.run(run) == []


def test_openai_codex_provider_retries_transient_http_opening_failure(tmp_path: Path) -> None:
    store = _store_with_oauth(tmp_path)
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, headers={"retry-after": "0"})
        return httpx.Response(
            200,
            text='data: {"type":"response.output_text.delta","delta":"recovered"}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICodexProvider(
                auth_resolver=StoredProviderAuthResolver(store),
                client=client,
                retry_policy=RetryPolicy(
                    max_retries=1,
                    base_delay_seconds=0.0001,
                    max_delay_seconds=1,
                ),
            )
            return [event async for event in provider.stream([Message(role="user", content="hi")])]

    events = anyio.run(run)

    assert attempts == 2
    assert isinstance(events[0], ProviderRetrying)
    assert events[0].reason == "server_error"
    assert events[0].status_code == 503
    assert events[1:] == [
        ProviderResponseStarted(model="gpt-5.6-sol"),
        ProviderTextDelta(delta="recovered"),
        ProviderResponseCompleted(content="recovered"),
    ]


def test_openai_codex_provider_does_not_retry_terminal_quota_error(tmp_path: Path) -> None:
    store = _store_with_oauth(tmp_path)
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, json={"error": {"code": "insufficient_quota"}})

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICodexProvider(
                auth_resolver=StoredProviderAuthResolver(store),
                client=client,
            )
            events: list[object] = []
            with pytest.raises(ProviderError, match=r"OpenAI Codex API error \(429\)"):
                async for event in provider.stream([Message(role="user", content="hi")]):
                    events.append(event)
            return events

    assert anyio.run(run) == []
    assert attempts == 1


def test_openai_codex_provider_raises_after_exhausting_opening_retries(tmp_path: Path) -> None:
    store = _store_with_oauth(tmp_path)
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICodexProvider(
                auth_resolver=StoredProviderAuthResolver(store),
                client=client,
                retry_policy=RetryPolicy(
                    max_retries=1,
                    base_delay_seconds=0.0001,
                    max_delay_seconds=1,
                ),
            )
            events: list[object] = []
            with pytest.raises(ProviderError, match=r"OpenAI Codex API error \(503\)"):
                async for event in provider.stream([Message(role="user", content="hi")]):
                    events.append(event)
            return events

    events = anyio.run(run)

    assert attempts == 2
    assert len(events) == 1
    assert isinstance(events[0], ProviderRetrying)


def test_openai_codex_provider_normalizes_post_start_sse_failure(tmp_path: Path) -> None:
    store = _store_with_oauth(tmp_path)

    async def run() -> list[object]:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                text="data: not-json\n\n",
                headers={"content-type": "text/event-stream"},
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            provider = OpenAICodexProvider(
                auth_resolver=StoredProviderAuthResolver(store),
                client=client,
            )
            return [event async for event in provider.stream([Message(role="user", content="hi")])]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="gpt-5.6-sol"),
        ProviderResponseFailed(message="Invalid OpenAI Codex SSE event: Expecting value"),
    ]


@pytest.mark.parametrize(
    ("native_event", "message"),
    [
        ({"type": "error", "message": "boom"}, "OpenAI Codex error: boom"),
        (
            {
                "type": "response.failed",
                "response": {"error": {"message": "boom"}},
            },
            "OpenAI Codex response failed: boom",
        ),
        (
            {
                "type": "response.incomplete",
                "response": {"incomplete_details": {"reason": "max_output_tokens"}},
            },
            "OpenAI Codex response incomplete: max_output_tokens",
        ),
    ],
)
def test_openai_codex_provider_emits_one_native_failed_terminal(
    tmp_path: Path,
    native_event: dict[str, object],
    message: str,
) -> None:
    provider = StubOpenAICodexProvider(
        [native_event],
        auth_resolver=StoredProviderAuthResolver(_store_with_oauth(tmp_path)),
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hi")])]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="gpt-test"),
        ProviderResponseFailed(message=message),
    ]


def _store_with_oauth(tmp_path: Path) -> JsonAuthStore:
    store = JsonAuthStore(tmp_path / "auth.json")
    store.set(
        "openai-codex",
        OAuthCredential(
            access=_fake_codex_token(),
            refresh="refresh-token",
            expires=4_102_444_800_000,
            account_id="account-id",
        ),
    )
    return store


def _fake_codex_token() -> str:
    header: dict[str, Any] = {"alg": "none"}
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": "account-id"}}
    return ".".join([_b64(header), _b64(payload), "signature"])


def _b64(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode("utf-8")).decode("ascii").rstrip("=")
