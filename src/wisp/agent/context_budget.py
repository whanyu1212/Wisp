"""Provider-neutral context estimation and budgeting."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from hashlib import sha256

from wisp.agent.messages import Message
from wisp.events import (
    ContextAccountingMethod,
    ContextBudget,
    ContextEstimate,
    ContextObservation,
)
from wisp.providers.base import ToolCallResult, ToolSpec


def estimate_context(
    messages: Sequence[Message],
    tools: Sequence[ToolSpec] = (),
    tool_results: Sequence[ToolCallResult] = (),
) -> ContextEstimate:
    """Estimate normalized request context with a stable UTF-8-bytes-per-token heuristic."""

    system_payloads = [
        _message_payload(message) for message in messages if message.role == "system"
    ]
    message_payloads = [
        _message_payload(message) for message in messages if message.role != "system"
    ]
    message_payloads.extend(
        {
            "call_id": result.call_id,
            "output": result.output,
            "is_error": result.is_error,
        }
        for result in tool_results
    )
    tool_payloads = [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
        for tool in tools
    ]
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
) -> ContextObservation:
    """Capture provider-reported input usage for one exact request context."""

    return ContextObservation(
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        message_count=len(messages),
        context_fingerprint=context_fingerprint(messages, tools),
    )


def trailing_context_estimate(
    messages: Sequence[Message],
    tools: Sequence[ToolSpec],
    observation: ContextObservation,
) -> ContextEstimate | None:
    """Estimate context appended after a still-valid observed request prefix."""

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
    """Build a budget, anchoring a valid prefix to provider-reported input usage."""

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
    """Combine an estimate with optional model-window and provider observations."""

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
    """Return a stable identity for the provider-visible context and tool schemas."""

    payload = {
        "messages": [_message_payload(message) for message in messages],
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in tools
        ],
    }
    text = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(_utf8_bytes(text)).hexdigest()


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
    "observe_context",
    "trailing_context_estimate",
]
