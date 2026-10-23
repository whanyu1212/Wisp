"""Provider-neutral context estimation and budgeting."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from hashlib import sha256

from wisp.events import (
    ContextAccountingMethod,
    ContextBudget,
    ContextEstimate,
    ContextObservation,
)
from wisp.messages import Message
from wisp.providers.base import ToolSpec


def estimate_context(
    messages: Sequence[Message],
    tools: Sequence[ToolSpec] = (),
) -> ContextEstimate:
    """Estimate normalized request context with a stable UTF-8-bytes-per-token heuristic.

    Args:
        messages (Sequence[Message]): Request history; system messages are counted apart.
        tools (Sequence[ToolSpec]): Tool schemas advertised with the request.

    Returns:
        ContextEstimate: System, message, and tool-schema tokens and their total.
    """

    system_payloads = [
        _message_payload(message) for message in messages if message.role == "system"
    ]
    message_payloads = [
        _message_payload(message) for message in messages if message.role != "system"
    ]
    tool_payloads = [_tool_payload(tool) for tool in tools]
    system_tokens = _payload_tokens(system_payloads)
    message_tokens = _payload_tokens(message_payloads)
    tool_schema_tokens = _payload_tokens(tool_payloads)
    return ContextEstimate(
        method="utf8_bytes_div_4_v2",
        system_tokens=system_tokens,
        message_tokens=message_tokens,
        tool_schema_tokens=tool_schema_tokens,
        total_tokens=system_tokens + message_tokens + tool_schema_tokens,
    )


def observe_context(
    messages: Sequence[Message],
    tools: Sequence[ToolSpec] = (),
    *,
    provider: str,
    model: str | None,
    input_tokens: int,
    prompt_cache_key: str | None = None,
) -> ContextObservation:
    """Capture provider-reported input usage for one exact request context.

    Args:
        messages (Sequence[Message]): Exact history the request sent.
        tools (Sequence[ToolSpec]): Tool schemas the request advertised.
        provider (str): Provider that reported the usage.
        model (str | None): Model the usage applies to.
        input_tokens (int): Provider-reported input tokens for the request.
        prompt_cache_key (str | None): Cache key the request carried, if any.

    Returns:
        ContextObservation: Usage keyed by a fingerprint of the messages and tools, so a
            later request can reuse it only while that prefix is unchanged.
    """

    return ContextObservation(
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        message_count=len(messages),
        context_fingerprint=context_fingerprint(messages, tools),
        prompt_cache_key=prompt_cache_key,
    )


def trailing_context_estimate(
    messages: Sequence[Message],
    tools: Sequence[ToolSpec],
    observation: ContextObservation,
) -> ContextEstimate | None:
    """Estimate context appended after a still-valid observed request prefix.

    Args:
        messages (Sequence[Message]): Current request history.
        tools (Sequence[ToolSpec]): Current tool schemas.
        observation (ContextObservation): Usage observed for an earlier request.

    Returns:
        ContextEstimate | None: Estimate for messages after the observed prefix, or None
            when the history is shorter than that prefix or its fingerprint changed.
    """

    if observation.message_count > len(messages):
        return None
    prefix = messages[: observation.message_count]
    if context_fingerprint(prefix, tools) != observation.context_fingerprint:
        return None
    return estimate_context(messages[observation.message_count :])


def estimate_context_budget(
    messages: Sequence[Message],
    tools: Sequence[ToolSpec] = (),
    *,
    context_window: int | None,
    reserve_tokens: int,
    observation: ContextObservation | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> ContextBudget:
    """Build a budget, anchoring a valid prefix to provider-reported input usage.

    Args:
        messages (Sequence[Message]): Request history to budget.
        tools (Sequence[ToolSpec]): Tool schemas advertised with the request.
        context_window (int | None): Model context capacity, when known.
        reserve_tokens (int): Tokens held back from the window.
        observation (ContextObservation | None): Latest provider-observed usage, if any.
        provider (str | None): Provider for the next request.
        model (str | None): Model for the next request.

    Returns:
        ContextBudget: Observed usage plus a trailing estimate when the observation
            matches this provider, model, and history prefix; otherwise a full estimate.

    Raises:
        ValueError: reserve_tokens is negative.
    """

    estimate = estimate_context(messages, tools)
    trailing = (
        trailing_context_estimate(messages, tools, observation)
        if observation is not None
        and observation.provider == provider
        and observation.model == model
        else None
    )
    return build_context_budget(
        estimate,
        context_window=context_window,
        reserve_tokens=reserve_tokens,
        observed_tokens=observation.input_tokens if observation is not None else None,
        observed_is_current=trailing is not None,
        trailing_estimated_tokens=trailing.total_tokens if trailing is not None else None,
    )


def build_context_budget(
    estimate: ContextEstimate,
    *,
    context_window: int | None,
    reserve_tokens: int,
    observed_tokens: int | None = None,
    observed_is_current: bool = False,
    trailing_estimated_tokens: int | None = None,
) -> ContextBudget:
    """Combine an estimate with optional model-window and provider observations.

    Args:
        estimate (ContextEstimate): Heuristic estimate for the whole request.
        context_window (int | None): Model context capacity; None omits window fields.
        reserve_tokens (int): Tokens held back from the window.
        observed_tokens (int | None): Provider-reported input tokens, if any.
        observed_is_current (bool): Whether the observation still covers this request's
            prefix.
        trailing_estimated_tokens (int | None): Estimate for messages after that prefix.

    Returns:
        ContextBudget: Effective tokens from the current observation plus its trailing
            estimate, or from the full estimate, with remaining-window fields when the
            window is known.

    Raises:
        ValueError: reserve_tokens is negative.
    """

    if reserve_tokens < 0:
        raise ValueError("reserve_tokens must be non-negative")
    if observed_is_current and observed_tokens is not None:
        trailing_tokens = trailing_estimated_tokens or 0
        effective_tokens = observed_tokens + trailing_tokens
        accounting_method: ContextAccountingMethod = (
            "provider_observed" if trailing_tokens == 0 else "provider_observed_plus_estimate"
        )
    else:
        trailing_tokens = None
        effective_tokens = estimate.total_tokens
        accounting_method = "fully_estimated"
    if context_window is None:
        return ContextBudget(
            estimate=estimate,
            observed_tokens=observed_tokens,
            observed_is_current=observed_is_current,
            trailing_estimated_tokens=trailing_tokens,
            effective_tokens=effective_tokens,
            accounting_method=accounting_method,
            reserve_tokens=reserve_tokens,
        )
    remaining = context_window - reserve_tokens - effective_tokens
    return ContextBudget(
        estimate=estimate,
        observed_tokens=observed_tokens,
        observed_is_current=observed_is_current,
        trailing_estimated_tokens=trailing_tokens,
        effective_tokens=effective_tokens,
        accounting_method=accounting_method,
        context_window=context_window,
        reserve_tokens=reserve_tokens,
        remaining_tokens=remaining,
        estimated_percent=(effective_tokens / context_window) * 100,
        over_budget=remaining <= 0,
    )


def context_fingerprint(
    messages: Sequence[Message],
    tools: Sequence[ToolSpec] = (),
) -> str:
    """Return a stable identity for the provider-visible context and tool schemas.

    Args:
        messages (Sequence[Message]): Messages whose provider-visible fields are hashed.
        tools (Sequence[ToolSpec]): Tool schemas included in the identity.

    Returns:
        str: SHA-256 hex digest of the canonical JSON payload.
    """

    payload = {
        "messages": [_message_payload(message) for message in messages],
        "tools": [_tool_payload(tool) for tool in tools],
    }
    text = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(_utf8_bytes(text)).hexdigest()


def message_tokens(message: Message) -> int:
    """Estimate one message alone, with the heuristic `estimate_context` uses.

    Args:
        message (Message): Message to measure.

    Returns:
        int: Estimated tokens. Summing these over messages slightly overcounts a
        joint estimate, which rounds once.
    """

    return _payload_tokens(_message_payload(message))


def tool_schema_tokens(tool: ToolSpec) -> int:
    """Estimate one tool's schema alone, with the heuristic `estimate_context` uses.

    Args:
        tool (ToolSpec): Tool whose name, description, and input schema to measure.

    Returns:
        int: Estimated tokens.
    """

    return _payload_tokens(_tool_payload(tool))


def text_tokens(text: str) -> int:
    """Estimate plain text with the UTF-8-bytes-per-token heuristic.

    Args:
        text (str): Text to measure, without JSON quoting.

    Returns:
        int: Estimated tokens; 0 for empty text.
    """

    return math.ceil(len(_utf8_bytes(text)) / 4)


def _tool_payload(tool: ToolSpec) -> dict[str, object]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
    }


def _message_payload(message: Message) -> dict[str, object]:
    payload: dict[str, object] = {"role": message.role, "content": message.content}
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_name is not None:
        payload["tool_name"] = message.tool_name
    if message.tool_calls:
        payload["tool_calls"] = [
            call.model_dump(mode="json", exclude_none=True) for call in message.tool_calls
        ]
    return payload


def _payload_tokens(payload: object) -> int:
    if payload == []:
        return 0
    text = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return math.ceil(len(_utf8_bytes(text)) / 4)


def _utf8_bytes(text: str) -> bytes:
    """Encode valid Unicode normally and preserve lone surrogates as JSON escapes."""

    return text.encode("utf-8", errors="backslashreplace")


__all__ = [
    "build_context_budget",
    "context_fingerprint",
    "estimate_context",
    "estimate_context_budget",
    "message_tokens",
    "observe_context",
    "text_tokens",
    "tool_schema_tokens",
    "trailing_context_estimate",
]
