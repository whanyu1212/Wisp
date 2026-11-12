"""Provider authentication resolution helpers."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import anyio

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
        # Requests that find the token expired at the same moment, as a run's
        # subagents starting together do, refresh it once between them.
        self._refresh_lock = anyio.Lock()

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
            refreshed = await self._refresh(provider, refresh)
            if refreshed is None:
                return None
            credential = refreshed
        return BearerTokenAuth(token=credential.access, account_id=credential.account_id)

    async def _refresh(
        self,
        provider: str,
        refresh: Callable[[OAuthCredential], Awaitable[OAuthCredential]],
    ) -> OAuthCredential | None:
        """Refresh a provider's expired OAuth credential, one request at a time.

        A refresh token is often good for one use, so two requests refreshing the
        same one could fail or end the sign-in. A request that waited for another's
        refresh uses the credential that one stored.

        Args:
            provider (str): Provider whose credential expired.
            refresh (Callable[[OAuthCredential], Awaitable[OAuthCredential]]):
                Exchanges the stored credential for a new one.

        Returns:
            OAuthCredential | None: A usable credential, or None when the stored one
            is no longer an OAuth credential, or another process replaced it with
            one that has also expired.
        """

        async with self._refresh_lock:
            credential = self.store.get(provider)
            if not isinstance(credential, OAuthCredential):
                return None
            if not _oauth_expired(credential, skew_seconds=self.expiry_skew_seconds):
                return credential
            refreshed = await refresh(credential)
            if self.store.compare_and_set(provider, expected=credential, replacement=refreshed):
                return refreshed
            current = self.store.get(provider)
            if not isinstance(current, OAuthCredential) or _oauth_expired(
                current, skew_seconds=self.expiry_skew_seconds
            ):
                return None
            return current


def _oauth_expired(credential: OAuthCredential, *, skew_seconds: int) -> bool:
    return int(time.time() * 1000) >= credential.expires - skew_seconds * 1000


__all__ = [
    "BearerTokenAuth",
    "ProviderAuthResolver",
    "StoredProviderAuthResolver",
]
