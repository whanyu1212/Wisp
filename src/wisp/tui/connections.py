"""Typed presentation data for Wisp's provider connection workflow."""

from wisp.auth.connections import (
    API_KEY_ENVIRONMENT_VARIABLES,
    OPENAI_COMPATIBLE_API_KEY_ENVIRONMENT_VARIABLE,
    ConnectionKind,
    ConnectionMethodStatus,
    ConnectionProviderStatus,
    ConnectionSource,
)

__all__ = [
    "API_KEY_ENVIRONMENT_VARIABLES",
    "ConnectionKind",
    "ConnectionMethodStatus",
    "ConnectionProviderStatus",
    "ConnectionSource",
    "OPENAI_COMPATIBLE_API_KEY_ENVIRONMENT_VARIABLE",
]
