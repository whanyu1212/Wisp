"""Provider protocol for model backends."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

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


class ContextOverflowError(ProviderError):
    """Raised when a provider rejects a request that exceeds its context window."""


_CONTEXT_OVERFLOW_MARKERS = (
    "context_length_exceeded",
    "context length exceeded",
    "context window exceeded",
    "maximum context length",
    "model_context_window_exceeded",
    "prompt is too long",
    "input token count exceeds",
    "input tokens exceed",
    "too many input tokens",
)


def is_context_overflow_message(message: str) -> bool:
    """Return whether a provider error message identifies a context overflow."""

    normalized = message.casefold()
    if any(marker in normalized for marker in _CONTEXT_OVERFLOW_MARKERS):
        return True
    return "input token count" in normalized and (
        "exceeds" in normalized or "maximum number of tokens" in normalized
    )


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
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Yield retry progress before one typed response lifecycle and terminal event.

        ``effort`` is a provider-native reasoning-effort tier string (e.g.
        Anthropic's ``"high"``, Google's ``"LOW"``, OpenAI's ``"medium"``) --
        not normalized across providers. A provider that receives a value it
        does not recognize, or ``None``, sends its own unmodified default
        behavior; providers with no effort concept ignore it entirely.
        """
        ...


class PromptCacheKeyProvider(Protocol):
    """Opt-in provider capability for request-level prompt-cache routing."""

    supports_prompt_cache_key: Literal[True]

    def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
        prompt_cache_key: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Yield one response lifecycle with an optional stable cache-routing key."""
        ...


class ContinuationMessageProvider(Protocol):
    """Opt-in capability to append user messages to an active continuation.

    The loop supplies ``extra_messages`` only when it is non-empty and this
    capability is declared. Implementations append those messages after the
    request's current tool results, preserve provider-native replay, and can
    continue clean responses when their cursor remains usable.
    """

    supports_continuation_messages: Literal[True]

    def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        extra_messages: Sequence[Message] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Yield one response lifecycle with new user messages appended once."""
        ...


class PromptCacheContinuationMessageProvider(Protocol):
    """Combined optional capability for adapters supporting both features."""

    supports_prompt_cache_key: Literal[True]
    supports_continuation_messages: Literal[True]

    def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        extra_messages: Sequence[Message] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
        prompt_cache_key: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Yield one response lifecycle with both optional features applied."""
        ...
