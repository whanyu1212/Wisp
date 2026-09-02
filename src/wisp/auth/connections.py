"""Backend-owned provider connection catalog and environment metadata."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from wisp.auth.storage import (
    ApiKeyCredential,
    AuthCredential,
    OAuthCredential,
)
from wisp.openai_compatible import openai_compatible_api_key_environment

type ConnectionKind = Literal["device_code", "api_key"]
type ConnectionSource = Literal["environment", "stored", "missing"]

API_KEY_ENVIRONMENT_VARIABLES = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "xai": ("XAI_API_KEY",),
}
OPENAI_COMPATIBLE_API_KEY_ENVIRONMENT_VARIABLE = "OPENAI_COMPATIBLE_API_KEY"
DEVICE_CODE_PROVIDER = "openai-codex"


class CredentialReader(Protocol):
    """Minimal credential lookup used to project a connection catalog."""

    def get(self, provider: str) -> AuthCredential | None: ...


@dataclass(frozen=True, slots=True)
class ConnectionMethodStatus:
    """One authentication method in the backend-owned connection catalog."""

    provider: str
    label: str
    kind: ConnectionKind
    source: ConnectionSource
    environment_variable: str | None = None
    oauth_expires_at: str | None = None
    has_stored_credential: bool = False

    @property
    def connected(self) -> bool:
        return self.source != "missing"


@dataclass(frozen=True, slots=True)
class ConnectionProviderStatus:
    """One provider family and its available authentication methods."""

    id: str
    label: str
    methods: tuple[ConnectionMethodStatus, ...]


def connection_catalog(
    store: CredentialReader,
    *,
    openai_compatible_provider: str = "openai-compatible",
    environ: Callable[[str], str | None] | None = None,
) -> tuple[ConnectionProviderStatus, ...]:
    """Return the sanitized connection catalog for the current auth store."""

    getenv = environ or _environment_value
    openai_codex = store.get(DEVICE_CODE_PROVIDER)
    return (
        ConnectionProviderStatus(
            id="openai",
            label="OpenAI",
            methods=(
                ConnectionMethodStatus(
                    provider=DEVICE_CODE_PROVIDER,
                    label="ChatGPT Plus/Pro",
                    kind="device_code",
                    source="stored" if isinstance(openai_codex, OAuthCredential) else "missing",
                    oauth_expires_at=_oauth_expiry_text(openai_codex),
                    has_stored_credential=isinstance(openai_codex, OAuthCredential),
                ),
                _api_key_method("openai", "OpenAI API key", store.get("openai"), getenv),
            ),
        ),
        ConnectionProviderStatus(
            id=openai_compatible_provider,
            label=openai_compatible_provider,
            methods=(
                _api_key_method(
                    openai_compatible_provider,
                    f"{openai_compatible_provider} API key",
                    store.get(openai_compatible_provider),
                    getenv,
                    openai_compatible_provider=openai_compatible_provider,
                ),
            ),
        ),
        ConnectionProviderStatus(
            id="xai",
            label="xAI",
            methods=(_api_key_method("xai", "xAI API key", store.get("xai"), getenv),),
        ),
        ConnectionProviderStatus(
            id="deepseek",
            label="DeepSeek",
            methods=(
                _api_key_method("deepseek", "DeepSeek API key", store.get("deepseek"), getenv),
            ),
        ),
        ConnectionProviderStatus(
            id="anthropic",
            label="Anthropic",
            methods=(
                _api_key_method("anthropic", "Anthropic API key", store.get("anthropic"), getenv),
            ),
        ),
        ConnectionProviderStatus(
            id="google",
            label="Google",
            methods=(_api_key_method("google", "Google API key", store.get("google"), getenv),),
        ),
    )


def connection_method(
    catalog: tuple[ConnectionProviderStatus, ...],
    provider: str,
) -> ConnectionMethodStatus | None:
    """Return the catalog method for ``provider``, if present."""

    return next(
        (method for family in catalog for method in family.methods if method.provider == provider),
        None,
    )


def supports_api_key(
    provider: str, *, openai_compatible_provider: str = "openai-compatible"
) -> bool:
    """Return whether ``provider`` accepts a stored API key."""

    return provider in API_KEY_ENVIRONMENT_VARIABLES or provider == openai_compatible_provider


def api_key_environment(
    provider: str,
    *,
    openai_compatible_provider: str = "openai-compatible",
) -> tuple[str, ...]:
    """Return environment variable names that can supply ``provider``'s API key."""

    if provider == openai_compatible_provider:
        provider_environment = openai_compatible_api_key_environment(provider)
        if provider_environment == OPENAI_COMPATIBLE_API_KEY_ENVIRONMENT_VARIABLE:
            return (provider_environment,)
        return (provider_environment, OPENAI_COMPATIBLE_API_KEY_ENVIRONMENT_VARIABLE)
    return API_KEY_ENVIRONMENT_VARIABLES.get(provider, ())


def configured_environment_variables(
    provider: str,
    *,
    openai_compatible_provider: str = "openai-compatible",
    environ: Callable[[str], str | None] | None = None,
) -> tuple[str, ...]:
    """Return configured API-key environment variable names, without values."""

    getenv = environ or _environment_value
    return tuple(
        name
        for name in api_key_environment(
            provider,
            openai_compatible_provider=openai_compatible_provider,
        )
        if getenv(name) is not None
    )


def auth_status_line(provider: str, method: ConnectionMethodStatus | None) -> str:
    """Return the sanitized `/auth` status line for one provider."""

    if method is None or method.source == "missing":
        return f"{provider}: not logged in"
    if method.source == "environment":
        names = method.environment_variable or "environment"
        return f"{provider}: api key configured via {names}"
    if method.kind == "device_code":
        expiry = f" ({method.oauth_expires_at})" if method.oauth_expires_at else ""
        return f"{provider}: oauth configured{expiry}"
    return f"{provider}: api key configured"


def _api_key_method(
    provider: str,
    label: str,
    credential: AuthCredential | None,
    getenv: Callable[[str], str | None],
    *,
    openai_compatible_provider: str = "openai-compatible",
) -> ConnectionMethodStatus:
    names = api_key_environment(
        provider,
        openai_compatible_provider=openai_compatible_provider,
    )
    environment_variable = next((name for name in names if getenv(name) is not None), None)
    if environment_variable is not None:
        source: ConnectionSource = "environment"
    elif isinstance(credential, ApiKeyCredential):
        source = "stored"
    else:
        source = "missing"
    return ConnectionMethodStatus(
        provider=provider,
        label=label,
        kind="api_key",
        source=source,
        environment_variable=environment_variable or (names[0] if names else None),
        has_stored_credential=isinstance(credential, ApiKeyCredential),
    )


def _environment_value(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _oauth_expiry_text(credential: AuthCredential | None) -> str | None:
    if not isinstance(credential, OAuthCredential):
        return None
    try:
        expires = datetime.fromtimestamp(credential.expires / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return f"expires {expires.isoformat()}"


__all__ = [
    "API_KEY_ENVIRONMENT_VARIABLES",
    "ConnectionKind",
    "ConnectionMethodStatus",
    "ConnectionProviderStatus",
    "ConnectionSource",
    "DEVICE_CODE_PROVIDER",
    "OPENAI_COMPATIBLE_API_KEY_ENVIRONMENT_VARIABLE",
    "api_key_environment",
    "auth_status_line",
    "configured_environment_variables",
    "connection_catalog",
    "connection_method",
    "supports_api_key",
]
