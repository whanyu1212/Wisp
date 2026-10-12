from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import anyio
import httpx
import pytest
from pytest import MonkeyPatch

from tests.providers.openai_codex_support import store_with_oauth
from wisp.agent.messages import Message
from wisp.auth import openai_codex as openai_codex_auth_module
from wisp.auth.storage import JsonAuthStore
from wisp.providers import openai_codex as openai_codex_module
from wisp.providers.auth import StoredProviderAuthResolver
from wisp.providers.base import ProviderConfigurationError
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderRetrying,
    ProviderTextDelta,
)
from wisp.providers.openai_codex import OpenAICodexProvider
from wisp.retry import RetryPolicy


def test_device_code_login_shields_owned_client_cleanup_from_cancellation(
    monkeypatch: MonkeyPatch,
) -> None:
    clients: list[StubDeviceCodeClient] = []

    class StubDeviceCodeClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.poll_started = anyio.Event()
            self.close_finished = anyio.Event()
            clients.append(self)

        async def post(self, url: str, **_kwargs: object) -> httpx.Response:
            if url == openai_codex_auth_module.DEVICE_USER_CODE_URL:
                return httpx.Response(
                    200,
                    json={
                        "device_auth_id": "device-id",
                        "user_code": "user-code",
                        "interval": 1,
                    },
                )
            self.poll_started.set()
            await anyio.sleep_forever()
            raise AssertionError("unreachable")

        async def aclose(self) -> None:
            await anyio.sleep(0)
            self.close_finished.set()

    monkeypatch.setattr(openai_codex_auth_module.httpx, "AsyncClient", StubDeviceCodeClient)

    async def run() -> bool:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(openai_codex_auth_module.login_openai_codex_device_code)
            while not clients:
                await anyio.sleep(0)
            await clients[0].poll_started.wait()
            task_group.cancel_scope.cancel()
        return clients[0].close_finished.is_set()

    assert anyio.run(run) is True


def test_device_code_poll_reports_progress_without_credentials(monkeypatch: MonkeyPatch) -> None:
    responses = [
        httpx.Response(403, json={}),
        httpx.Response(
            200,
            json={"authorization_code": "authorization", "code_verifier": "verifier"},
        ),
    ]

    class StubPollClient:
        async def post(self, _url: str, **_kwargs: object) -> httpx.Response:
            return responses.pop(0)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(openai_codex_auth_module.anyio, "sleep", no_sleep)
    progress: list[int] = []

    async def run() -> tuple[str, str]:
        return await openai_codex_auth_module._poll_device_code(
            cast(httpx.AsyncClient, StubPollClient()),
            device_auth_id="device-id",
            user_code="user-code",
            interval_seconds=1,
            on_progress=progress.append,
        )

    assert anyio.run(run) == ("authorization", "verifier")
    assert progress == [1]


def test_device_code_poll_has_a_bounded_timeout(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(openai_codex_auth_module, "DEVICE_CODE_TIMEOUT_SECONDS", 0)

    class NeverPollClient:
        async def post(self, _url: str, **_kwargs: object) -> httpx.Response:
            raise AssertionError("timeout must be checked before polling")

    async def run() -> None:
        with pytest.raises(RuntimeError, match="timed out"):
            await openai_codex_auth_module._poll_device_code(
                cast(httpx.AsyncClient, NeverPollClient()),
                device_auth_id="device-id",
                user_code="user-code",
                interval_seconds=1,
            )

    anyio.run(run)


def test_openai_codex_provider_requires_login(tmp_path: Path) -> None:
    provider = OpenAICodexProvider(
        auth_resolver=StoredProviderAuthResolver(JsonAuthStore(tmp_path / "auth.json"))
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hi")])]

    with pytest.raises(ProviderConfigurationError, match=r"/connect openai-codex"):
        anyio.run(run)


def test_openai_codex_provider_does_not_start_before_http_success(tmp_path: Path) -> None:
    store = store_with_oauth(tmp_path)

    async def run() -> list[object]:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(401, text="invalid credentials")
        )
        async with httpx.AsyncClient(transport=transport) as client:
            provider = OpenAICodexProvider(
                auth_resolver=StoredProviderAuthResolver(store),
                client=client,
            )
            return [event async for event in provider.stream([Message(role="user", content="hi")])]

    events = anyio.run(run)
    assert len(events) == 1
    assert isinstance(events[0], ProviderResponseFailed)
    assert "OpenAI Codex API error (401)" in events[0].message


def test_openai_codex_provider_retries_transient_http_opening_failure(tmp_path: Path) -> None:
    store = store_with_oauth(tmp_path)
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, headers={"retry-after": "0"})
        return httpx.Response(
            200,
            text=(
                'data: {"type":"response.output_text.delta","delta":"recovered"}\n\n'
                'data: {"type":"response.completed","response":{}}\n\n'
            ),
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


class _ReadTimeoutStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield (
            b'data: {"type":"response.created","response":{"id":"response-id"}}\n\n'
            b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
        )
        raise httpx.ReadTimeout("Codex stream stalled")


def test_openai_codex_provider_does_not_retry_post_start_read_timeout(tmp_path: Path) -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            200,
            stream=_ReadTimeoutStream(),
            headers={"content-type": "text/event-stream"},
        )

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICodexProvider(
                auth_resolver=StoredProviderAuthResolver(store_with_oauth(tmp_path)),
                client=client,
                retry_policy=RetryPolicy(
                    max_retries=1,
                    base_delay_seconds=0.0001,
                    max_delay_seconds=1,
                ),
            )
            return [event async for event in provider.stream([Message(role="user", content="hi")])]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="gpt-5.6-sol"),
        ProviderTextDelta(delta="partial"),
        ProviderResponseFailed(
            message="Codex stream stalled",
            partial_content="partial",
            response_id="response-id",
        ),
    ]
    assert attempts == 1


def test_openai_codex_provider_configures_owned_client_timeouts(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    real_async_client = httpx.AsyncClient
    created_client: httpx.AsyncClient | None = None
    seen_timeout: httpx.Timeout | None = None

    def create_client(*, timeout: httpx.Timeout) -> httpx.AsyncClient:
        nonlocal created_client, seen_timeout
        seen_timeout = timeout
        created_client = real_async_client(
            timeout=timeout,
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    text='data: {"type":"response.completed","response":{}}\n\n',
                    headers={"content-type": "text/event-stream"},
                )
            ),
        )
        return created_client

    monkeypatch.setattr(openai_codex_module.httpx, "AsyncClient", create_client)
    provider = OpenAICodexProvider(
        auth_resolver=StoredProviderAuthResolver(store_with_oauth(tmp_path))
    )

    async def run() -> list[object]:
        return [event async for event in provider.stream([Message(role="user", content="hi")])]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="gpt-5.6-sol"),
        ProviderResponseCompleted(content=""),
    ]
    assert seen_timeout is not None
    assert seen_timeout.connect == 10.0
    assert seen_timeout.read == 300.0
    assert seen_timeout.write == 30.0
    assert seen_timeout.pool == 10.0
    assert created_client is not None
    assert created_client.is_closed


def test_openai_codex_provider_does_not_retry_terminal_quota_error(tmp_path: Path) -> None:
    store = store_with_oauth(tmp_path)
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
            return [event async for event in provider.stream([Message(role="user", content="hi")])]

    events = anyio.run(run)
    assert len(events) == 1
    assert isinstance(events[0], ProviderResponseFailed)
    assert "OpenAI Codex API error (429)" in events[0].message
    assert attempts == 1


def test_openai_codex_provider_raises_after_exhausting_opening_retries(tmp_path: Path) -> None:
    store = store_with_oauth(tmp_path)
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
            return [event async for event in provider.stream([Message(role="user", content="hi")])]

    events = anyio.run(run)

    assert attempts == 2
    assert len(events) == 2
    assert isinstance(events[0], ProviderRetrying)
    assert isinstance(events[1], ProviderResponseFailed)
