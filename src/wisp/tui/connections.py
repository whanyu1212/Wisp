"""Typed presentation data for Wisp's provider connection workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type ConnectionKind = Literal["device_code", "api_key"]
type ConnectionSource = Literal["environment", "stored", "missing"]


@dataclass(frozen=True, slots=True)
class ConnectionMethodStatus:
    """One authentication method displayed by the connection panel."""

    provider: str
    label: str
    kind: ConnectionKind
    source: ConnectionSource
    environment_variable: str | None = None

    @property
    def connected(self) -> bool:
        return self.source != "missing"


@dataclass(frozen=True, slots=True)
class ConnectionProviderStatus:
    """One provider family and its available authentication methods."""

    id: str
    label: str
    methods: tuple[ConnectionMethodStatus, ...]


API_KEY_ENVIRONMENT_VARIABLES = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
}
OPENAI_COMPATIBLE_API_KEY_ENVIRONMENT_VARIABLE = "OPENAI_COMPATIBLE_API_KEY"


__all__ = [
    "API_KEY_ENVIRONMENT_VARIABLES",
    "ConnectionKind",
    "ConnectionMethodStatus",
    "ConnectionProviderStatus",
    "ConnectionSource",
    "OPENAI_COMPATIBLE_API_KEY_ENVIRONMENT_VARIABLE",
]
