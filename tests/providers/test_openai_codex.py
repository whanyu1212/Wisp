from __future__ import annotations

from pathlib import Path

import anyio
import httpx
import pytest

from tests.providers.openai_codex_support import (
    StubOpenAICodexProvider,
    completed_event,
    fake_codex_token,
    store_with_oauth,
)
from wisp.messages import Message
from wisp.providers.auth import StoredProviderAuthResolver
from wisp.providers.base import ToolCall
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderToolCallCompleted,
)
from wisp.providers.openai_codex import OpenAICodexProvider


def test_openai_codex_provider_streams_text_with_subscription_headers(tmp_path: Path) -> None:
    store = store_with_oauth(tmp_path)
    provider = StubOpenAICodexProvider(
        [
            {"type": "response.created", "response": {"id": "response-id"}},
            {"type": "response.output_text.delta", "delta": "hello"},
            {"type": "response.output_text.delta", "delta": " world"},
            completed_event("response-id"),
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
    assert provider.seen_headers["Authorization"] == f"Bearer {fake_codex_token()}"
    assert provider.seen_headers["chatgpt-account-id"] == "account-id"
    assert provider.seen_headers["OpenAI-Beta"] == "responses=experimental"


def test_openai_codex_provider_yields_tool_calls(tmp_path: Path) -> None:
    store = store_with_oauth(tmp_path)
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
            completed_event("response-id"),
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


@pytest.mark.production_fault
def test_openai_codex_provider_rejects_eof_without_native_completion(tmp_path: Path) -> None:
    provider = StubOpenAICodexProvider(
        [
            {"type": "response.created", "response": {"id": "response-id"}},
            {"type": "response.output_text.delta", "delta": "partial"},
        ],
        auth_resolver=StoredProviderAuthResolver(store_with_oauth(tmp_path)),
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hi")])]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="gpt-test"),
        ProviderTextDelta(delta="partial"),
        ProviderResponseFailed(
            message="OpenAI Codex stream ended before response.completed was received",
            partial_content="partial",
            response_id="response-id",
        ),
    ]


def test_openai_codex_provider_does_not_expose_tool_calls_before_completion(
    tmp_path: Path,
) -> None:
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
        auth_resolver=StoredProviderAuthResolver(store_with_oauth(tmp_path)),
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hi")])]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="gpt-test"),
        ProviderResponseFailed(
            message="OpenAI Codex stream ended before response.completed was received",
            response_id="response-id",
        ),
    ]


@pytest.mark.production_fault
def test_openai_codex_provider_rejects_done_without_native_completion(tmp_path: Path) -> None:
    store = store_with_oauth(tmp_path)

    async def run() -> list[object]:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                text=(
                    'data: {"type":"response.created","response":{"id":"response-id"}}\n\n'
                    'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
                    "data: [DONE]\n\n"
                ),
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
        ProviderTextDelta(delta="partial"),
        ProviderResponseFailed(
            message="OpenAI Codex stream ended before response.completed was received",
            partial_content="partial",
            response_id="response-id",
        ),
    ]


def test_openai_codex_provider_normalizes_post_start_sse_failure(tmp_path: Path) -> None:
    store = store_with_oauth(tmp_path)

    async def run() -> tuple[list[object], object]:
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
            provider._continuations.remember("previous-response", ())  # noqa: SLF001
            events = [
                event
                async for event in provider.stream(
                    [Message(role="user", content="hi")],
                    previous_response_id="previous-response",
                )
            ]
            remaining = provider._continuations.get("previous-response")  # noqa: SLF001
            return events, remaining

    events, remaining = anyio.run(run)
    assert events == [
        ProviderResponseStarted(model="gpt-5.6-sol"),
        ProviderResponseFailed(
            message="Invalid OpenAI Codex SSE event: Expecting value",
            response_id="previous-response",
        ),
    ]
    assert remaining is None


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
        auth_resolver=StoredProviderAuthResolver(store_with_oauth(tmp_path)),
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hi")])]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="gpt-test"),
        ProviderResponseFailed(message=message),
    ]
