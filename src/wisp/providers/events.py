"""Provider-neutral response events emitted by model adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from json import JSONDecodeError, loads
from typing import Literal, cast

JsonObject = Mapping[str, object]
ProviderFinishReason = Literal["stop", "tool_calls", "length"]
ProviderFailureKind = Literal["error", "context_overflow", "aborted"]
RetryReason = Literal["network", "timeout", "rate_limit", "server_error", "transient_http"]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """Provider-agnostic request from a model to call a tool."""

    call_id: str
    name: str
    arguments: JsonObject
    raw_arguments: str = ""
    response_id: str | None = None
    # Some providers assign a local fallback so Wisp can correlate execution
    # results even when their wire protocol omitted a function-call ID. Keep
    # the original provider-issued ID separately for faithful fresh replay.
    provider_call_id: str | None = field(default=None, compare=False)
    parse_error: str | None = None


def parse_tool_arguments(*, name: str, raw_arguments: str) -> tuple[JsonObject, str | None]:
    """Parse a tool call's streamed JSON arguments.

    Args:
        name (str): Tool name, used in the error text.
        raw_arguments (str): The arguments exactly as the model streamed them;
            empty means no arguments.

    Returns:
        tuple[JsonObject, str | None]: The arguments and None, or an empty object
        and the reason the arguments are unusable.
    """
    try:
        parsed = loads(raw_arguments or "{}")
    except JSONDecodeError as exc:
        return {}, f"Invalid JSON arguments for tool {name}: {exc.msg}"
    if not isinstance(parsed, dict):
        return {}, f"Invalid JSON arguments for tool {name}: expected an object"
    return cast(JsonObject, parsed), None


def parse_tool_call(
    *,
    call_id: str,
    name: str,
    raw_arguments: str,
    response_id: str | None,
) -> ToolCall:
    """Build a tool call, keeping unparseable arguments as a parse error.

    Args:
        call_id (str): Provider-issued call ID.
        name (str): Tool name.
        raw_arguments (str): The arguments exactly as the model streamed them.
        response_id (str | None): Response that produced the call.

    Returns:
        ToolCall: The call, with `parse_error` set when `raw_arguments` is not a
        JSON object.
    """
    arguments, parse_error = parse_tool_arguments(name=name, raw_arguments=raw_arguments)
    return ToolCall(
        call_id=call_id,
        name=name,
        arguments=arguments,
        raw_arguments=raw_arguments,
        response_id=response_id,
        parse_error=parse_error,
    )


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
    """A complete observed call; the terminal finish reason controls execution eligibility."""

    tool_call: ToolCall
    content_index: int = 0


@dataclass(frozen=True, slots=True)
class ProviderWebSource:
    """One page a provider-native web search returned or opened."""

    url: str
    title: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderWebSearch:
    """A web search the provider ran on its own servers during the response.

    The provider already gave the results to the model; this event only reports
    what was searched so frontends can show it. ``action`` is ``"search"`` for
    a query, ``"open_page"`` when the model opened a URL, and ``"find_in_page"``
    when it searched within a page (``query`` then holds the pattern).
    ``error`` carries the provider's error code when the search failed.
    """

    action: Literal["search", "open_page", "find_in_page"] = "search"
    query: str | None = None
    sources: tuple[ProviderWebSource, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Provider-reported token usage for one successful API request."""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    context_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderResponseCompleted:
    """The terminal successful response, including its complete assistant state.

    ``length`` may retain observed tool calls so the agent loop can reject each call
    in-band without executing it. The loop validates every finish-reason/tool-call
    combination before entering the executor or approval policy.
    """

    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    response_id: str | None = None
    finish_reason: ProviderFinishReason = "stop"
    usage: ProviderUsage | None = None


@dataclass(frozen=True, slots=True)
class ProviderResponseFailed:
    """The terminal failed response, preserving any partial assistant text."""

    message: str
    partial_content: str = ""
    response_id: str | None = None
    failure_kind: ProviderFailureKind = "error"


type ProviderEvent = (
    ProviderResponseStarted
    | ProviderRetrying
    | ProviderTextDelta
    | ProviderThinkingDelta
    | ProviderToolCallCompleted
    | ProviderWebSearch
    | ProviderResponseCompleted
    | ProviderResponseFailed
)


__all__ = [
    "JsonObject",
    "ProviderEvent",
    "ProviderFailureKind",
    "ProviderFinishReason",
    "ProviderResponseCompleted",
    "ProviderResponseFailed",
    "ProviderResponseStarted",
    "ProviderRetrying",
    "ProviderTextDelta",
    "ProviderThinkingDelta",
    "ProviderToolCallCompleted",
    "ProviderUsage",
    "ProviderWebSearch",
    "ProviderWebSource",
    "ToolCall",
    "RetryReason",
    "parse_tool_arguments",
    "parse_tool_call",
]
