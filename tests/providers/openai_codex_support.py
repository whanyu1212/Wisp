"""Stub provider, OAuth store, and response events shared by the OpenAI Codex provider tests."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from wisp.auth.storage import JsonAuthStore, OAuthCredential
from wisp.providers.auth import StoredProviderAuthResolver
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


def store_with_oauth(tmp_path: Path) -> JsonAuthStore:
    store = JsonAuthStore(tmp_path / "auth.json")
    store.set(
        "openai-codex",
        OAuthCredential(
            access=fake_codex_token(),
            refresh="refresh-token",
            expires=4_102_444_800_000,
            account_id="account-id",
        ),
    )
    return store


def completed_event(response_id: str) -> dict[str, object]:
    return {"type": "response.completed", "response": {"id": response_id}}


def fake_codex_token() -> str:
    header: dict[str, Any] = {"alg": "none"}
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": "account-id"}}
    return ".".join([_b64(header), _b64(payload), "signature"])


def _b64(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode("utf-8")).decode("ascii").rstrip("=")
