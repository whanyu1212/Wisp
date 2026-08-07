"""Provider authentication resolution helpers."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from wisp.auth.storage import ApiKeyCredential, JsonAuthStore, OAuthCredential

DEFAULT_TOKEN_EXPIRY_SKEW_SECONDS = 60


@dataclass(frozen=True)
class BearerTokenAuth:
    """Resolved bearer-token auth for a provider request."""

    token: str
    account_id: str | None = None


class ProviderAuthResolver(Protocol):
    """Resolves provider credentials for request-time use."""

    async def api_key(self, provider: str) -> str | None:
        """Return a stored API key for a provider, if present."""
        ...

    async def bearer_token(
        self,
        provider: str,
        *,
        refresh: Callable[[OAuthCredential], Awaitable[OAuthCredential]] | None = None,
    ) -> BearerTokenAuth | None:
        """Return bearer-token auth for a provider, refreshing OAuth if needed."""
        ...


class StoredProviderAuthResolver:
    """Resolve provider auth from Wisp's private auth store."""

    def __init__(
        self,
        store: JsonAuthStore,
        *,
        expiry_skew_seconds: int = DEFAULT_TOKEN_EXPIRY_SKEW_SECONDS,
    ) -> None:
        self.store = store
        self.expiry_skew_seconds = expiry_skew_seconds

    async def api_key(self, provider: str) -> str | None:
        credential = self.store.get(provider)
        if isinstance(credential, ApiKeyCredential):
            return credential.key
        return None

    async def bearer_token(
        self,
        provider: str,
        *,
        refresh: Callable[[OAuthCredential], Awaitable[OAuthCredential]] | None = None,
    ) -> BearerTokenAuth | None:
        credential = self.store.get(provider)
        if credential is None:
            return None
        if isinstance(credential, ApiKeyCredential):
            return BearerTokenAuth(token=credential.key)
        if _oauth_expired(credential, skew_seconds=self.expiry_skew_seconds):
            if refresh is None:
                return None
            credential = await refresh(credential)
            self.store.set(provider, credential)
        return BearerTokenAuth(token=credential.access, account_id=credential.account_id)


def _oauth_expired(credential: OAuthCredential, *, skew_seconds: int) -> bool:
    return int(time.time() * 1000) >= credential.expires - skew_seconds * 1000


__all__ = [
    "BearerTokenAuth",
    "ProviderAuthResolver",
    "StoredProviderAuthResolver",
]
