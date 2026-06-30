"""Provider protocol for model backends."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from wisp.agent.messages import Message


class ProviderError(RuntimeError):
    """Base error raised by model providers."""


class ProviderConfigurationError(ProviderError):
    """Raised when a provider is selected but not configured correctly."""


class Provider(Protocol):
    """Minimal streaming provider contract used by the agent core."""

    name: str
    default_model: str | None

    def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
    ) -> AsyncIterator[str]:
        """Yield text deltas for the assistant response."""
        ...
