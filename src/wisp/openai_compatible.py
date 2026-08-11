"""User-owned configuration for OpenAI-compatible Chat Completions endpoints."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, field_validator


class OpenAICompatibleSettings(BaseModel):
    """Configuration for one OpenAI-compatible Chat Completions endpoint.

    This value is user-only because changing ``base_url`` redirects requests carrying
    the user's credential. API keys deliberately live outside this model.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    base_url: str
    default_model: str
    requires_api_key: bool = True

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


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


__all__ = ["OpenAICompatibleSettings"]
