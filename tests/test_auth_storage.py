from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import anyio

from wisp.auth.storage import ApiKeyCredential, JsonAuthStore, OAuthCredential
from wisp.providers.auth import StoredProviderAuthResolver


def test_json_auth_store_writes_private_oauth_credentials(tmp_path: Path) -> None:
    path = tmp_path / "wisp" / "auth.json"
    store = JsonAuthStore(path)

    store.set(
        "openai-codex",
        OAuthCredential(
            access="access-token",
            refresh="refresh-token",
            expires=4_102_444_800_000,
            account_id="account-id",
        ),
    )

    assert store.providers() == ("openai-codex",)
    assert store.get("openai-codex") == OAuthCredential(
        access="access-token",
        refresh="refresh-token",
        expires=4_102_444_800_000,
        account_id="account-id",
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["openai-codex"]["type"] == "oauth"
    assert raw["openai-codex"]["accountId"] == "account-id"
    if os.name == "posix":
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_json_auth_store_deletes_provider(tmp_path: Path) -> None:
    store = JsonAuthStore(tmp_path / "auth.json")
    store.set("openai", ApiKeyCredential(key="sk-test"))

    assert store.delete("openai") is True
    assert store.delete("openai") is False
    assert store.get("openai") is None


def test_stored_provider_auth_resolver_refreshes_expired_oauth(tmp_path: Path) -> None:
    store = JsonAuthStore(tmp_path / "auth.json")
    store.set(
        "openai-codex",
        OAuthCredential(
            access="old-access",
            refresh="old-refresh",
            expires=0,
            account_id="old-account",
        ),
    )
    resolver = StoredProviderAuthResolver(store)

    async def refresh(credential: OAuthCredential) -> OAuthCredential:
        assert credential.access == "old-access"
        return OAuthCredential(
            access="new-access",
            refresh="new-refresh",
            expires=4_102_444_800_000,
            account_id="new-account",
        )

    async def run() -> None:
        auth = await resolver.bearer_token("openai-codex", refresh=refresh)
        assert auth is not None
        assert auth.token == "new-access"
        assert auth.account_id == "new-account"

    anyio.run(run)
    assert store.get("openai-codex") == OAuthCredential(
        access="new-access",
        refresh="new-refresh",
        expires=4_102_444_800_000,
        account_id="new-account",
    )


def test_stored_provider_auth_resolver_resolves_only_api_keys(tmp_path: Path) -> None:
    store = JsonAuthStore(tmp_path / "auth.json")
    store.set("openai", ApiKeyCredential(key="stored-key"))
    store.set(
        "openai-codex",
        OAuthCredential(access="access", refresh="refresh", expires=4_102_444_800_000),
    )
    resolver = StoredProviderAuthResolver(store)

    async def run() -> None:
        assert await resolver.api_key("openai") == "stored-key"
        assert await resolver.api_key("openai-codex") is None
        assert await resolver.api_key("missing") is None

    anyio.run(run)
