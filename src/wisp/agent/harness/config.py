"""Dependencies, runtime limits, and queue policy for AgentHarness."""

from __future__ import annotations

from dataclasses import dataclass

from wisp.agent.loop import UsageCostEstimator
from wisp.agent.tool_contracts import ToolExecutor
from wisp.agent.validation import validate_agent_runtime_limits
from wisp.events import QueueMode
from wisp.providers.base import Provider, ToolSpec

_MAX_PENDING_QUEUE_MESSAGES = 100
_MAX_PENDING_QUEUE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class AgentHarnessConfig:
    """Portable dependencies and limits for an `AgentHarness`."""

    provider: Provider
    tool_executor: ToolExecutor
    model: str | None = None
    tools: tuple[ToolSpec, ...] = ()
    max_tool_iterations: int | None = None
    effort: str | None = None
    context_window: int | None = None
    context_reserve_tokens: int = 16_384
    context_pressure_threshold: float = 0.8
    cost_estimator: UsageCostEstimator | None = None
    steering_mode: QueueMode = "one_at_a_time"
    follow_up_mode: QueueMode = "one_at_a_time"
    max_pending_queue_messages: int = _MAX_PENDING_QUEUE_MESSAGES
    max_pending_queue_bytes: int = _MAX_PENDING_QUEUE_BYTES
    prompt_cache_key: str | None = None

    def __post_init__(self) -> None:
        """Reject invalid runtime settings even when callers bypass static typing."""
        validate_agent_runtime_limits(
            max_tool_iterations=self.max_tool_iterations,
            context_window=self.context_window,
            context_reserve_tokens=self.context_reserve_tokens,
            context_pressure_threshold=self.context_pressure_threshold,
        )
        _require_queue_mode(self.steering_mode)
        _require_queue_mode(self.follow_up_mode)
        if type(self.max_pending_queue_messages) is not int or self.max_pending_queue_messages < 0:
            raise ValueError("max_pending_queue_messages must be a non-negative integer")
        if type(self.max_pending_queue_bytes) is not int or self.max_pending_queue_bytes < 0:
            raise ValueError("max_pending_queue_bytes must be a non-negative integer")


def _require_queue_mode(mode: object) -> None:
    if not isinstance(mode, str) or mode not in {"one_at_a_time", "all"}:
        raise ValueError(f"Unsupported queue mode: {mode!r}")
