"""Provider protocol for model backends."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from wisp.agent.messages import Message


class Provider(Protocol):
    """Minimal streaming provider contract used by the agent core."""

    name: str

    def stream(self, messages: Sequence[Message]) -> AsyncIterator[str]:
        """Yield text deltas for the assistant response."""
        ...
