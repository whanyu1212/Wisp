"""User-owned configuration for OpenAI-compatible Chat Completions endpoints."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, field_validator

_RESERVED_PROVIDER_NAMES = frozenset({"anthropic", "fake", "google", "openai", "openai-codex"})
_PROVIDER_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_PROVIDER_NAME_MAX_LENGTH = 64


class OpenAICompatibleSettings(BaseModel):
    """Configuration for one OpenAI-compatible Chat Completions endpoint.

    This value is user-only because changing ``base_url`` redirects requests carrying
    the user's credential. API keys deliberately live outside this model.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    provider_name: str = "openai-compatible"
    base_url: str
    default_model: str
    requires_api_key: bool = True
    ca_bundle: Path | None = None

    @field_validator("provider_name")
    @classmethod
    def _validate_provider_name(cls, value: str) -> str:
        return validate_openai_compatible_provider_name(value)

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP or HTTPS URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query or fragment")
        if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
            raise ValueError("unencrypted HTTP is allowed only for loopback endpoints")
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))

    @field_validator("default_model")
    @classmethod
    def _validate_default_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("default_model must not be blank")
        return normalized

    @field_validator("ca_bundle")
    @classmethod
    def _validate_ca_bundle(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        expanded = value.expanduser()
        if not expanded.is_absolute():
            raise ValueError("ca_bundle must be an absolute path")
        resolved = expanded.resolve(strict=False)
        if not resolved.is_file():
            raise ValueError("ca_bundle must be an existing file")
        return resolved


def validate_openai_compatible_provider_name(value: str) -> str:
    """Normalize and validate a custom provider name used in commands and model selectors."""

    normalized = value.strip()
    if not normalized:
        raise ValueError("provider_name must not be blank")
    if len(normalized) > _PROVIDER_NAME_MAX_LENGTH or not _PROVIDER_NAME_PATTERN.fullmatch(
        normalized
    ):
        raise ValueError(
            "provider_name must start with a lowercase letter and contain only lowercase "
            "letters, digits, or single hyphens"
        )
    if normalized in _RESERVED_PROVIDER_NAMES:
        raise ValueError(f"provider_name conflicts with built-in provider {normalized!r}")
    return normalized


def openai_compatible_api_key_environment(provider_name: str) -> str:
    """Return the provider-specific API-key environment variable name."""

    validated = validate_openai_compatible_provider_name(provider_name)
    return f"{validated.replace('-', '_').upper()}_API_KEY"


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


__all__ = [
    "OpenAICompatibleSettings",
    "openai_compatible_api_key_environment",
    "validate_openai_compatible_provider_name",
]
