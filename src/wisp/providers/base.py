"""Provider protocol for model backends."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from wisp.agent.messages import Message
from wisp.providers.events import JsonObject, ProviderEvent
from wisp.providers.events import ToolCall as ToolCall
from wisp.tools.base import Tool


@dataclass(frozen=True)
class ToolSpec:
    """Provider-agnostic description of a callable tool."""

    name: str
    description: str
    input_schema: JsonObject

    @classmethod
    def from_tool(cls, tool: Tool) -> ToolSpec:
        """Create a provider-facing spec from a registered runtime tool."""

        return cls(
            name=tool.name,
            description=tool.description,
            input_schema=tool.input_schema,
        )


@dataclass(frozen=True)
class ToolCallResult:
    """Provider-agnostic result returned for a model tool call."""

    call_id: str
    output: str
    is_error: bool = False


class ProviderError(RuntimeError):
    """Base error raised by model providers."""


class ProviderConfigurationError(ProviderError):
    """Raised when a provider is selected but not configured correctly."""


class ProviderProtocolError(ProviderError):
    """Raised when a provider emits a malformed response event sequence."""


class Provider(Protocol):
    """Minimal streaming provider contract used by the agent core."""

    name: str
    default_model: str | None

    def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Yield one typed, terminal provider-response event stream."""
        ...
