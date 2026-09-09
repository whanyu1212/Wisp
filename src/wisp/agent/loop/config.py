"""Configuration contracts for the provider-neutral agent loop."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from wisp.agent.request_boundary import ContextOverflowHook, RequestBoundaryHook
from wisp.agent.tool_contracts import ToolExecutor
from wisp.agent.validation import validate_agent_runtime_limits, validate_non_negative_integer
from wisp.events import TokenUsage, UsageCost
from wisp.providers.base import Provider, ToolSpec

type UsageCostEstimator = Callable[[str, str | None, str | None, TokenUsage], UsageCost]


class CancellationToken(Protocol):
    """Cooperative cancellation state observed by the pure loop."""

    def is_cancelled(self) -> bool:
        """Check whether cancellation has been requested.

        Returns:
            bool: True when the loop should stop at its next cancellation check.
        """
        ...


@dataclass(frozen=True, slots=True)
class AgentLoopConfig:
    """Configure the dependencies and limits for one provider-neutral loop run.

    Args:
        provider (Provider): Adapter that streams model responses.
        tool_executor (ToolExecutor): Executor responsible for tool policy and execution.
        model (str | None, optional): Requested model; None uses the provider default.
        tools (tuple[ToolSpec, ...], optional): Tool schemas advertised to the model.
        max_tool_iterations (int | None, optional): Maximum tool batches, including the
            iteration offset. None leaves the count unlimited.
        cancellation_token (CancellationToken | None, optional): Cooperative stop signal.
        effort (str | None, optional): Provider-native reasoning tier, forwarded unchanged.
        context_window (int | None, optional): Context capacity in tokens, when known.
        context_reserve_tokens (int, optional): Tokens reserved in budget estimates.
            Defaults to 16,384.
        context_pressure_threshold (float, optional): Input-token fraction that triggers
            a pressure event. Defaults to 0.8.
        turn_offset (int, optional): Number of turns preceding this invocation.
        tool_iteration_offset (int, optional): Tool batches already consumed.
        cost_estimator (UsageCostEstimator | None, optional): Optional usage-pricing callback.
        defer_context_overflow_errors (bool, optional): Leave terminal overflow error
            events to the caller. Does not suppress every raised overflow exception.
        prompt_cache_key (str | None, optional): Cache key sent to supporting adapters.
        request_boundary_hook (RequestBoundaryHook | None, optional): Policy consulted
            after a successful turn to stop, continue, or change request context.
        context_overflow_hook (ContextOverflowHook | None, optional): Policy that can
            replace or rebase rejected context before retrying in the same loop.
    """

    provider: Provider
    tool_executor: ToolExecutor
    model: str | None = None
    tools: tuple[ToolSpec, ...] = ()
    max_tool_iterations: int | None = None
    cancellation_token: CancellationToken | None = None
    # Provider-native reasoning-effort tier string (e.g. Anthropic's "high",
    # Google's "MEDIUM", OpenAI's "low") -- not normalized across providers,
    # forwarded to Provider.stream() as-is. None means "use the provider's
    # own default behavior."
    effort: str | None = None
    context_window: int | None = None
    context_reserve_tokens: int = 16_384
    context_pressure_threshold: float = 0.8
    turn_offset: int = 0
    tool_iteration_offset: int = 0
    cost_estimator: UsageCostEstimator | None = None
    defer_context_overflow_errors: bool = False
    prompt_cache_key: str | None = None
    request_boundary_hook: RequestBoundaryHook | None = None
    context_overflow_hook: ContextOverflowHook | None = None

    def __post_init__(self) -> None:
        """Validate runtime limits and continuation offsets after construction.

        Raises:
            ValueError: A limit, threshold, or offset is outside its accepted range
                or has an unsupported type.
        """
        validate_agent_runtime_limits(
            max_tool_iterations=self.max_tool_iterations,
            context_window=self.context_window,
            context_reserve_tokens=self.context_reserve_tokens,
            context_pressure_threshold=self.context_pressure_threshold,
        )
        validate_non_negative_integer(self.turn_offset, field="turn_offset")
        validate_non_negative_integer(self.tool_iteration_offset, field="tool_iteration_offset")


__all__ = ["AgentLoopConfig", "CancellationToken", "UsageCostEstimator"]
