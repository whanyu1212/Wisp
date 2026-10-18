from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import cast

import anyio
import httpx
import pytest
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pytest import MonkeyPatch

from tests.providers.google_support import (
    StubGenaiClient,
    StubModels,
    text_chunk,
)
from wisp.auth.storage import ApiKeyCredential, JsonAuthStore
from wisp.messages import Message as WispMessage
from wisp.providers.auth import StoredProviderAuthResolver
from wisp.providers.base import (
    ProviderConfigurationError,
    ToolCallResult,
    ToolSpec,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderRetrying,
    ProviderTextDelta,
)
from wisp.providers.google import GoogleProvider
from wisp.retry import RetryPolicy


class FlakyGoogleProvider(GoogleProvider):
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
        effort: str | None = None,
    ) -> AsyncIterator[genai_types.GenerateContentResponse]:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise httpx.ConnectError("boom")

        async def stream() -> AsyncIterator[genai_types.GenerateContentResponse]:
            yield text_chunk("recovered", finish_reason=genai_types.FinishReason.STOP)

        return stream()


def test_google_provider_retries_request_opening_failure_before_start() -> None:
    provider = FlakyGoogleProvider(
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


def test_google_provider_raises_after_exhausting_opening_retries() -> None:
    provider = FlakyGoogleProvider(
        2,
        retry_policy=RetryPolicy(max_retries=1, base_delay_seconds=0.0001, max_delay_seconds=1),
    )

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    events = anyio.run(run)

    assert provider.attempts == 2
    assert len(events) == 2
    assert isinstance(events[0], ProviderRetrying)
    assert isinstance(events[1], ProviderResponseFailed)


def test_wisp_owned_google_client_leaves_sdk_retries_unset() -> None:
    # google-genai defaults retry_options to None, which its own retry_args()
    # resolves to "never retry" (stop_after_attempt(1)) -- Wisp owns retries
    # via RetryPolicy, so no explicit opt-out is needed, unlike the
    # max_retries=0 construction AnthropicProvider/OpenAIProvider require.
    provider = GoogleProvider(api_key="test-key")

    async def run() -> genai.Client:
        return await provider._client_or_create()  # noqa: SLF001

    client = anyio.run(run)

    assert client._api_client._http_options.retry_options is None  # noqa: SLF001
    client.close()


def test_google_provider_uses_and_rotates_stored_api_key(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    store = JsonAuthStore(tmp_path / "auth.json")
    store.set("google", ApiKeyCredential(key="old-stored-key"))
    provider = GoogleProvider(auth_resolver=StoredProviderAuthResolver(store))

    async def run() -> tuple[genai.Client, genai.Client]:
        old_client = await provider._client_or_create()  # noqa: SLF001
        store.set("google", ApiKeyCredential(key="new-stored-key"))
        new_client = await provider._client_or_create()  # noqa: SLF001
        return old_client, new_client

    old_client, new_client = anyio.run(run)

    assert old_client._api_client.api_key == "old-stored-key"  # noqa: SLF001
    assert new_client._api_client.api_key == "new-stored-key"  # noqa: SLF001
    assert new_client is not old_client
    assert old_client.aio._api_client._async_httpx_client.is_closed  # noqa: SLF001
    new_client.close()


def test_google_provider_api_key_precedence(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    store = JsonAuthStore(tmp_path / "auth.json")
    store.set("google", ApiKeyCredential(key="stored-key"))
    resolver = StoredProviderAuthResolver(store)
    monkeypatch.setenv("GOOGLE_API_KEY", "env-key")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")

    async def run() -> tuple[genai.Client, genai.Client, genai.Client]:
        explicit_client = await GoogleProvider(
            api_key="explicit-key", auth_resolver=resolver
        )._client_or_create()  # noqa: SLF001
        env_client = await GoogleProvider(auth_resolver=resolver)._client_or_create()  # noqa: SLF001
        monkeypatch.delenv("GOOGLE_API_KEY")
        monkeypatch.delenv("GEMINI_API_KEY")
        stored_client = await GoogleProvider(auth_resolver=resolver)._client_or_create()  # noqa: SLF001
        return explicit_client, env_client, stored_client

    explicit_client, env_client, stored_client = anyio.run(run)

    assert explicit_client._api_client.api_key == "explicit-key"  # noqa: SLF001
    assert env_client._api_client.api_key == "env-key"  # noqa: SLF001
    assert stored_client._api_client.api_key == "stored-key"  # noqa: SLF001
    explicit_client.close()
    env_client.close()
    stored_client.close()


def test_google_provider_does_not_read_store_for_higher_priority_api_keys(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text("{invalid", encoding="utf-8")
    resolver = StoredProviderAuthResolver(JsonAuthStore(auth_path))
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    async def run() -> tuple[genai.Client, genai.Client]:
        explicit_client = await GoogleProvider(
            api_key="explicit-key", auth_resolver=resolver
        )._client_or_create()  # noqa: SLF001
        monkeypatch.setenv("GEMINI_API_KEY", "env-key")
        env_client = await GoogleProvider(auth_resolver=resolver)._client_or_create()  # noqa: SLF001
        return explicit_client, env_client

    explicit_client, env_client = anyio.run(run)

    assert explicit_client._api_client.api_key == "explicit-key"  # noqa: SLF001
    assert env_client._api_client.api_key == "env-key"  # noqa: SLF001
    explicit_client.close()
    env_client.close()


def test_google_provider_does_not_replace_injected_client(tmp_path: Path) -> None:
    store = JsonAuthStore(tmp_path / "auth.json")
    store.set("google", ApiKeyCredential(key="old-stored-key"))
    injected = cast(genai.Client, StubGenaiClient(StubModels()))
    provider = GoogleProvider(
        client=injected,
        auth_resolver=StoredProviderAuthResolver(store),
    )

    async def run() -> None:
        assert await provider._client_or_create() is injected  # noqa: SLF001
        store.set("google", ApiKeyCredential(key="new-stored-key"))
        assert await provider._client_or_create() is injected  # noqa: SLF001

    anyio.run(run)


def test_google_provider_requires_api_key(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    provider = GoogleProvider()

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    with pytest.raises(
        ProviderConfigurationError, match=r"/connect.*GOOGLE_API_KEY or GEMINI_API_KEY"
    ):
        anyio.run(run)


def test_google_provider_falls_back_to_gemini_api_key(monkeypatch: MonkeyPatch) -> None:
    # Regression test: mirrors google.genai's own env-var resolution order
    # (get_env_api_key) -- a user who already has Gemini configured with only
    # GEMINI_API_KEY should not be rejected before the SDK ever sees it.
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-env-key")
    provider = GoogleProvider()

    async def run() -> genai.Client:
        return await provider._client_or_create()  # noqa: SLF001

    client = anyio.run(run)

    assert client._api_client.api_key == "gemini-env-key"  # noqa: SLF001
    client.close()


def test_google_provider_prefers_google_api_key_over_gemini_api_key(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "google-env-key")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-env-key")
    provider = GoogleProvider()

    async def run() -> genai.Client:
        return await provider._client_or_create()  # noqa: SLF001

    client = anyio.run(run)

    assert client._api_client.api_key == "google-env-key"  # noqa: SLF001
    client.close()


def test_google_retry_decision_classifies_client_and_server_errors() -> None:
    from wisp.providers.google import _google_retry_decision

    timeout_decision = _google_retry_decision(httpx.TimeoutException("slow"))
    assert timeout_decision is not None
    assert timeout_decision.reason == "timeout"

    connect_decision = _google_retry_decision(httpx.ConnectError("down"))
    assert connect_decision is not None
    assert connect_decision.reason == "network"

    server_error = genai_errors.ServerError(
        503,
        {"error": {"message": "unavailable", "status": "UNAVAILABLE"}},
        response=httpx.Response(503, headers={}, request=httpx.Request("POST", "https://x")),
    )
    server_decision = _google_retry_decision(server_error)
    assert server_decision is not None
    assert server_decision.reason == "server_error"

    client_error = genai_errors.ClientError(
        400,
        {"error": {"message": "bad request", "status": "INVALID_ARGUMENT"}},
        response=httpx.Response(400, headers={}, request=httpx.Request("POST", "https://x")),
    )
    assert _google_retry_decision(client_error) is None
