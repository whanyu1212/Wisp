"""Provider-neutral response events emitted by model adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

JsonObject = Mapping[str, object]
ProviderFinishReason = Literal["stop", "tool_calls", "length"]
RetryReason = Literal["network", "timeout", "rate_limit", "server_error", "transient_http"]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """Provider-agnostic request from a model to call a tool."""

    call_id: str
    name: str
    arguments: JsonObject
    raw_arguments: str = ""
    response_id: str | None = None
    parse_error: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderResponseStarted:
    """A provider accepted a request and started a model response."""

    model: str
    response_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderRetrying:
    """A request-opening failure is waiting before its next provider attempt."""

    attempt: int
    max_attempts: int
    delay_seconds: float
    reason: RetryReason
    status_code: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderTextDelta:
    """A streamed assistant text fragment."""

    delta: str
    content_index: int = 0


@dataclass(frozen=True, slots=True)
class ProviderThinkingDelta:
    """A streamed assistant reasoning fragment."""

    delta: str
    content_index: int = 0


@dataclass(frozen=True, slots=True)
class ProviderToolCallCompleted:
    """A complete tool call parsed from the provider stream."""

    tool_call: ToolCall
    content_index: int = 0


@dataclass(frozen=True, slots=True)
class ProviderResponseCompleted:
    """The terminal successful response, including its complete assistant state."""

    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    response_id: str | None = None
    finish_reason: ProviderFinishReason = "stop"


@dataclass(frozen=True, slots=True)
class ProviderResponseFailed:
    """The terminal failed response, preserving any partial assistant text."""

    message: str
    partial_content: str = ""
    response_id: str | None = None


type ProviderEvent = (
    ProviderResponseStarted
    | ProviderRetrying
    | ProviderTextDelta
    | ProviderThinkingDelta
    | ProviderToolCallCompleted
    | ProviderResponseCompleted
    | ProviderResponseFailed
)


__all__ = [
    "JsonObject",
    "ProviderEvent",
    "ProviderFinishReason",
    "ProviderResponseCompleted",
    "ProviderResponseFailed",
    "ProviderResponseStarted",
    "ProviderRetrying",
    "ProviderTextDelta",
    "ProviderThinkingDelta",
    "ProviderToolCallCompleted",
    "ToolCall",
    "RetryReason",
]
