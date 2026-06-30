"""Provider protocol for model backends."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from wisp.agent.messages import Message
from wisp.tools.base import Tool

JsonObject = Mapping[str, object]


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
class ToolCall:
    """Provider-agnostic request from a model to call a tool."""

    call_id: str
    name: str
    arguments: JsonObject


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
    ) -> AsyncIterator[str]:
        """Yield text deltas for the assistant response."""
        ...
