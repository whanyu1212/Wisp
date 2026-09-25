"""Project validated provider responses into public and continuation messages."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from wisp.agent.context_budget import observe_context
from wisp.agent.messages import Message
from wisp.events import MessageCompleted, TokenUsage, ToolCallSnapshot, UsageCost

from .provider_lifecycle import CompletedProviderResponse
from .provider_request import provider_supports_prompt_cache_key

if TYPE_CHECKING:
    from .config import AgentLoopConfig


def unavailable_cost(
    provider: str,
    requested_model: str | None,
    response_model: str | None,
    *,
    reason: Literal["pricing_unavailable", "usage_incomplete", "estimation_failed"],
) -> UsageCost:
    """Build cost metadata that explains why a price could not be calculated.

    Args:
        provider (str): Provider identifier.
        requested_model (str | None): Model requested by the caller.
        response_model (str | None): Model reported by the provider, if available.
        reason (Literal["pricing_unavailable", "usage_incomplete", "estimation_failed"]):
            Reason pricing is unavailable.

    Returns:
        UsageCost: Unpriced metadata using the reported model, or requested model as fallback.
    """

    return UsageCost(
        provider=provider,
        requested_model=requested_model,
        model=response_model or requested_model,
        unavailable_reason=reason,
    )


def project_usage_and_cost(
    config: AgentLoopConfig,
    completed: CompletedProviderResponse,
) -> tuple[TokenUsage | None, UsageCost]:
    """Project usage and price a completed response without failing on missing pricing.

    Args:
        config (AgentLoopConfig): Provider identity, requested model, and cost estimator.
        completed (CompletedProviderResponse): Validated response with optional token usage.

    Returns:
        tuple[TokenUsage | None, UsageCost]: Usage when reported, plus priced or explicitly
            unavailable cost metadata. Estimator exceptions become estimation_failed.
    """

    response = completed.response
    usage = (
        TokenUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            total_tokens=response.usage.total_tokens,
            cache_read_input_tokens=response.usage.cache_read_input_tokens,
            cache_write_input_tokens=response.usage.cache_write_input_tokens,
            reasoning_output_tokens=response.usage.reasoning_output_tokens,
        )
        if response.usage is not None
        else None
    )
    if usage is None:
        cost = unavailable_cost(
            config.provider.name,
            config.model,
            completed.response_model,
            reason="usage_incomplete",
        )
    elif config.cost_estimator is None:
        cost = unavailable_cost(
            config.provider.name,
            config.model,
            completed.response_model,
            reason="pricing_unavailable",
        )
    else:
        try:
            cost = config.cost_estimator(
                config.provider.name,
                config.model,
                completed.response_model,
                usage,
            )
        except Exception:
            cost = unavailable_cost(
                config.provider.name,
                config.model,
                completed.response_model,
                reason="estimation_failed",
            )
    return usage, cost


@dataclass(frozen=True, slots=True)
class CompletedResponseProjection:
    """Public completion plus tool snapshots isolated before the public yield."""

    event: MessageCompleted
    continuation_tool_calls: tuple[ToolCallSnapshot, ...]

    def to_continuation_message(self) -> Message:
        """Build the live transcript row after the completion consumer resumes.

        Returns:
            Message: Assistant row created now, using completion metadata and the tool
                snapshots isolated before the public event was yielded.
        """

        event = self.event
        return Message(
            role="assistant",
            content=event.content,
            response_id=event.response_id,
            finish_reason=event.finish_reason,
            usage=event.usage,
            cost=event.cost,
            context_observation=event.context_observation,
            tool_calls=self.continuation_tool_calls,
        )


def project_completed_response(
    config: AgentLoopConfig,
    completed: CompletedProviderResponse,
    *,
    turn: int,
    request_messages: Sequence[Message],
    selected_model: str | None,
) -> CompletedResponseProjection:
    """Prepare public completion metadata and isolated continuation tool snapshots.

    Args:
        config (AgentLoopConfig): Provider, tools, and optional usage-pricing callback.
        completed (CompletedProviderResponse): Validated successful response.
        turn (int): Turn number attached to the public completion event.
        request_messages (Sequence[Message]): Exact portable context used for observation.
        selected_model (str | None): Requested model or provider default for the observation.

    Returns:
        CompletedResponseProjection: Public event and deep-copied continuation tool
            snapshots. The continuation Message is constructed later by the caller.

    Examples:
        Preserve tool arguments across a consumer-controlled yield boundary::

            projection = project_completed_response(
                config, completed, turn=turn,
                request_messages=request_messages, selected_model=selected_model,
            )
            yield projection.event
            continuation = projection.to_continuation_message()

        The observation prefers context_input_tokens when reported. Pressure events
        are calculated separately by the runner from usage.input_tokens.
    """

    response = completed.response
    usage, cost = project_usage_and_cost(config, completed)
    tool_call_snapshots = tuple(
        ToolCallSnapshot(
            call_id=tool_call.call_id,
            name=tool_call.name,
            arguments=deepcopy(dict(tool_call.arguments)),
            provider_call_id=tool_call.provider_call_id,
            parse_error=tool_call.parse_error,
        )
        for tool_call in response.tool_calls
    )
    # Copy before yielding: public arguments can contain mutable nested values.
    continuation_tool_calls = tuple(
        snapshot.model_copy(deep=True) for snapshot in tool_call_snapshots
    )
    context_observation = (
        observe_context(
            request_messages,
            config.tools,
            provider=config.provider.name,
            model=selected_model,
            input_tokens=(
                response.usage.context_input_tokens
                if response.usage is not None and response.usage.context_input_tokens is not None
                else usage.input_tokens
            ),
            # Record only a key the request actually carried: adapters without the
            # capability are opened without it (see provider_request).
            prompt_cache_key=(
                config.prompt_cache_key
                if provider_supports_prompt_cache_key(config.provider)
                else None
            ),
        )
        if usage is not None
        else None
    )
    return CompletedResponseProjection(
        event=MessageCompleted(
            turn=turn,
            content=completed.content,
            finish_reason=response.finish_reason,
            response_id=completed.response_id,
            usage=usage,
            cost=cost,
            context_observation=context_observation,
            tool_calls=tool_call_snapshots,
        ),
        continuation_tool_calls=continuation_tool_calls,
    )
