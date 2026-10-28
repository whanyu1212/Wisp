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
from wisp.auth.storage import JsonAuthStore, OAuthCredential
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
            message=(
                "OpenAI Codex ProviderError: Invalid OpenAI Codex SSE event: Expecting value "
                "(attempt 1). The response stream had started; it was not automatically retried. "
                "Resume the session to continue; "
                "inspect completed tool results before rerunning work."
            ),
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


def _stream(
    provider: OpenAICodexProvider, *, previous_response_id: str | None = None
) -> list[object]:
    async def run() -> list[object]:
        return [
            event
            async for event in provider.stream(
                [Message(role="user", content="hi")], previous_response_id=previous_response_id
            )
        ]

    return anyio.run(run)


def test_streamed_tool_call_is_emitted_and_replayed_once(tmp_path: Path) -> None:
    item = {
        "type": "function_call",
        "id": "item-id",
        "call_id": "call-id",
        "name": "lookup",
        "arguments": "",
    }
    provider = StubOpenAICodexProvider(
        [
            {"type": "response.created", "response": {"id": "response-id"}},
            {"type": "response.output_item.added", "item": item},
            {
                "type": "response.function_call_arguments.done",
                "item_id": "item-id",
                "arguments": '{"query":"wisp"}',
            },
            {
                "type": "response.output_item.done",
                "item": {**item, "arguments": '{"query":"wisp"}'},
            },
            {
                "type": "response.completed",
                "response": {
                    "id": "response-id",
                    "output": [{**item, "arguments": '{"query":"wisp"}'}],
                },
            },
        ],
        auth_resolver=StoredProviderAuthResolver(store_with_oauth(tmp_path)),
    )

    events = _stream(provider)

    completed = [event for event in events if isinstance(event, ProviderToolCallCompleted)]
    assert [event.tool_call.arguments for event in completed] == [{"query": "wisp"}]
    native = provider.native_output_for("response-id")
    assert native is not None
    assert [replayed["type"] for replayed in native.items] == ["function_call"]


def test_tool_call_without_item_id_keeps_its_arguments(tmp_path: Path) -> None:
    provider = StubOpenAICodexProvider(
        [
            {"type": "response.created", "response": {"id": "response-id"}},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": "call-id",
                    "name": "lookup",
                    "arguments": '{"query":"wisp"}',
                },
            },
            completed_event("response-id"),
        ],
        auth_resolver=StoredProviderAuthResolver(store_with_oauth(tmp_path)),
    )

    completed = [e for e in _stream(provider) if isinstance(e, ProviderToolCallCompleted)]

    assert [event.tool_call.arguments for event in completed] == [{"query": "wisp"}]


_ANONYMOUS_REASONING = [
    {"type": "reasoning", "encrypted_content": "first"},
    {"type": "reasoning", "encrypted_content": "second"},
]


@pytest.mark.parametrize(
    "events",
    [
        [
            *({"type": "response.output_item.done", "item": item} for item in _ANONYMOUS_REASONING),
            completed_event("response-id"),
        ],
        [
            {
                "type": "response.completed",
                "response": {"id": "response-id", "output": _ANONYMOUS_REASONING},
            }
        ],
    ],
    ids=["streamed", "completed-output"],
)
def test_output_items_without_ids_are_all_replayed(
    tmp_path: Path, events: list[dict[str, object]]
) -> None:
    provider = StubOpenAICodexProvider(
        events, auth_resolver=StoredProviderAuthResolver(store_with_oauth(tmp_path))
    )

    _stream(provider)

    native = provider.native_output_for("response-id")
    assert native is not None
    assert list(native.items) == _ANONYMOUS_REASONING


@pytest.mark.parametrize(
    "native_event",
    [
        {"type": "error", "message": "boom"},
        {"type": "response.failed", "response": {"error": {"message": "boom"}}},
        {"type": "response.incomplete", "response": {"incomplete_details": {"reason": "x"}}},
    ],
    ids=["error", "failed", "incomplete"],
)
def test_failure_names_the_started_response(
    tmp_path: Path, native_event: dict[str, object]
) -> None:
    provider = StubOpenAICodexProvider(
        [{"type": "response.created", "response": {"id": "response-id"}}, native_event],
        auth_resolver=StoredProviderAuthResolver(store_with_oauth(tmp_path)),
    )
    provider._continuations.remember("previous-response", ())  # noqa: SLF001

    failure = _stream(provider, previous_response_id="previous-response")[-1]

    assert isinstance(failure, ProviderResponseFailed)
    assert failure.response_id == "response-id"


def test_stored_account_id_wins_over_the_token(tmp_path: Path) -> None:
    store = JsonAuthStore(tmp_path / "auth.json")
    store.set(
        "openai-codex",
        OAuthCredential(
            access=fake_codex_token(),
            refresh="refresh-token",
            expires=4_102_444_800_000,
            account_id="stored-account-id",
        ),
    )
    provider = StubOpenAICodexProvider(
        [completed_event("response-id")], auth_resolver=StoredProviderAuthResolver(store)
    )

    _stream(provider)

    assert provider.seen_headers is not None
    assert provider.seen_headers["chatgpt-account-id"] == "stored-account-id"
