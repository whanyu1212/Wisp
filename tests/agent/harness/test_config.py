from __future__ import annotations

from dataclasses import MISSING, fields
from typing import cast

import pytest

from tests.agent.harness.support import (
    RecordingToolExecutor,
)
from wisp.agent.harness import AgentHarnessConfig, RunCancellationToken
from wisp.agent.harness.config import _build_loop_config
from wisp.agent.loop import AgentLoopConfig
from wisp.agent.request_boundary import RequestBoundaryDecision, RequestBoundarySnapshot
from wisp.events import TokenUsage, UsageCost
from wisp.providers.base import ToolSpec
from wisp.providers.fake import ScriptedProvider

# Loop fields that describe one invocation rather than the harness configuration.
_INVOCATION_FIELDS = {
    "cancellation_token",
    "turn_offset",
    "tool_iteration_offset",
    "request_boundary_hook",
    "context_overflow_hook",
}


class _StopBoundary:
    async def before_next_request(
        self, *, snapshot: RequestBoundarySnapshot
    ) -> RequestBoundaryDecision:
        return RequestBoundaryDecision(stop=True)


def _estimate_cost(
    provider: str, requested_model: str | None, model: str | None, usage: TokenUsage
) -> UsageCost:
    return UsageCost(provider=provider, requested_model=requested_model, model=model)


def test_positional_field_order_is_stable() -> None:
    config = AgentHarnessConfig(
        ScriptedProvider([]),
        RecordingToolExecutor(),
        None,
        (),
        None,
        None,
        None,
        16_384,
        0.8,
        None,
        "all",
        "all",
        0,
    )

    assert config.max_pending_queue_messages == 0
    assert config.prompt_cache_key is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_tool_iterations", -1),
        ("context_window", 0),
        ("context_reserve_tokens", True),
        ("context_pressure_threshold", float("inf")),
    ],
)
def test_rejects_invalid_runtime_limits(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        AgentHarnessConfig(
            provider=ScriptedProvider([]),
            tool_executor=RecordingToolExecutor(),
            **cast(dict[str, object], {field: value}),
        )


def test_accepts_runtime_limits_at_their_bounds() -> None:
    config = AgentHarnessConfig(
        provider=ScriptedProvider([]),
        tool_executor=RecordingToolExecutor(),
        max_tool_iterations=0,
        context_window=1,
        context_reserve_tokens=1,
        context_pressure_threshold=1,
    )

    assert config.max_tool_iterations == 0


def test_loop_config_forwards_every_field() -> None:
    harness_config = AgentHarnessConfig(
        provider=ScriptedProvider([]),
        tool_executor=RecordingToolExecutor(),
        model="model",
        tools=(ToolSpec(name="lookup", description="Look up", input_schema={"type": "object"}),),
        max_tool_iterations=3,
        effort="high",
        context_window=1_000,
        context_reserve_tokens=10,
        context_pressure_threshold=0.5,
        cost_estimator=_estimate_cost,
        prompt_cache_key="cache",
        web_search=True,
    )
    loop_config = _build_loop_config(
        harness_config,
        cancellation_token=RunCancellationToken(),
        turn_offset=0,
        tool_iteration_offset=0,
        request_boundary_hook=_StopBoundary(),
        context_overflow_hook=None,
    )

    harness_fields = {field.name: field for field in fields(AgentHarnessConfig)}
    shared_names = {field.name for field in fields(AgentLoopConfig)} - _INVOCATION_FIELDS
    assert shared_names <= harness_fields.keys()
    for name in shared_names:
        value = getattr(harness_config, name)
        # A default value would pass even if the field were not forwarded.
        assert value != harness_fields[name].default or harness_fields[name].default is MISSING
        assert getattr(loop_config, name) == value, name
