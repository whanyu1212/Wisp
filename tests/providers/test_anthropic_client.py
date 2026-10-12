from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import cast

import anyio
import httpx
import pytest
from anthropic import APIConnectionError, AsyncAnthropic
from anthropic.types import RawMessageStreamEvent
from pytest import MonkeyPatch

from tests.providers.anthropic_support import (
    StubAsyncAnthropic,
    StubMessagesResource,
    message_delta_event,
    text_delta_event,
)
from wisp.agent.messages import Message as WispMessage
from wisp.auth.storage import ApiKeyCredential, JsonAuthStore
from wisp.providers.anthropic import AnthropicProvider
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
from wisp.retry import RetryPolicy


class FlakyAnthropicProvider(AnthropicProvider):
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
    ) -> AsyncIterator[RawMessageStreamEvent]:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))

        async def stream() -> AsyncIterator[RawMessageStreamEvent]:
            yield text_delta_event("recovered")
            yield message_delta_event("end_turn")

        return stream()


def test_anthropic_provider_retries_request_opening_failure_before_start() -> None:
    provider = FlakyAnthropicProvider(
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


def test_anthropic_provider_raises_after_exhausting_opening_retries() -> None:
    provider = FlakyAnthropicProvider(
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


def test_wisp_owned_anthropic_client_disables_sdk_retries() -> None:
    provider = AnthropicProvider(api_key="test-key")

    async def run() -> None:
        client = await provider._client_or_create()  # noqa: SLF001
        assert client.max_retries == 0
        await client.close()

    anyio.run(run)


def test_anthropic_provider_uses_and_rotates_stored_api_key(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store = JsonAuthStore(tmp_path / "auth.json")
    store.set("anthropic", ApiKeyCredential(key="old-stored-key"))
    provider = AnthropicProvider(auth_resolver=StoredProviderAuthResolver(store))

    async def run() -> None:
        old_client = await provider._client_or_create()  # noqa: SLF001
        store.set("anthropic", ApiKeyCredential(key="new-stored-key"))
        new_client = await provider._client_or_create()  # noqa: SLF001

        assert old_client.api_key == "old-stored-key"
        assert new_client.api_key == "new-stored-key"
        assert new_client is not old_client
        assert old_client.is_closed()
        await new_client.close()

    anyio.run(run)


def test_anthropic_provider_api_key_precedence(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    store = JsonAuthStore(tmp_path / "auth.json")
    store.set("anthropic", ApiKeyCredential(key="stored-key"))
    resolver = StoredProviderAuthResolver(store)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")

    async def run() -> None:
        explicit_client = await AnthropicProvider(
            api_key="explicit-key", auth_resolver=resolver
        )._client_or_create()  # noqa: SLF001
        env_client = await AnthropicProvider(auth_resolver=resolver)._client_or_create()  # noqa: SLF001
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        stored_client = await AnthropicProvider(auth_resolver=resolver)._client_or_create()  # noqa: SLF001

        assert explicit_client.api_key == "explicit-key"
        assert env_client.api_key == "env-key"
        assert stored_client.api_key == "stored-key"
        await explicit_client.close()
        await env_client.close()
        await stored_client.close()

    anyio.run(run)


def test_anthropic_provider_does_not_read_store_for_higher_priority_api_keys(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text("{invalid", encoding="utf-8")
    resolver = StoredProviderAuthResolver(JsonAuthStore(auth_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    async def run() -> None:
        explicit_client = await AnthropicProvider(
            api_key="explicit-key", auth_resolver=resolver
        )._client_or_create()  # noqa: SLF001
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
        env_client = await AnthropicProvider(auth_resolver=resolver)._client_or_create()  # noqa: SLF001

        assert explicit_client.api_key == "explicit-key"
        assert env_client.api_key == "env-key"
        await explicit_client.close()
        await env_client.close()

    anyio.run(run)


def test_anthropic_provider_does_not_replace_injected_client(tmp_path: Path) -> None:
    store = JsonAuthStore(tmp_path / "auth.json")
    store.set("anthropic", ApiKeyCredential(key="old-stored-key"))
    injected = cast(AsyncAnthropic, StubAsyncAnthropic(StubMessagesResource()))
    provider = AnthropicProvider(
        client=injected,
        auth_resolver=StoredProviderAuthResolver(store),
    )

    async def run() -> None:
        assert await provider._client_or_create() is injected  # noqa: SLF001
        store.set("anthropic", ApiKeyCredential(key="new-stored-key"))
        assert await provider._client_or_create() is injected  # noqa: SLF001

    anyio.run(run)


def test_anthropic_provider_requires_api_key(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = AnthropicProvider()

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    with pytest.raises(ProviderConfigurationError, match=r"/connect.*ANTHROPIC_API_KEY"):
        anyio.run(run)
