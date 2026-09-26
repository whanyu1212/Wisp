from __future__ import annotations

from typing import cast

import pytest

from tests.agent.loop.support import (
    NeverToolExecutor,
)
from wisp.agent.loop import AgentLoopConfig
from wisp.providers.fake import ScriptedProvider


def test_positional_field_order_is_stable() -> None:
    config = AgentLoopConfig(
        ScriptedProvider([]),
        NeverToolExecutor(),
        None,
        (),
        None,
        None,
        None,
        None,
        16_384,
        0.8,
        0,
        0,
        None,
        "cache-key",
    )

    assert config.prompt_cache_key == "cache-key"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("max_tool_iterations", -1, "max_tool_iterations"),
        ("max_tool_iterations", True, "max_tool_iterations"),
        ("context_window", 0, "context_window"),
        ("context_window", True, "context_window"),
        ("context_reserve_tokens", -1, "context_reserve_tokens"),
        ("context_reserve_tokens", False, "context_reserve_tokens"),
        ("context_pressure_threshold", 0, "context_pressure_threshold"),
        ("context_pressure_threshold", 1.1, "context_pressure_threshold"),
        ("context_pressure_threshold", 10**1000, "context_pressure_threshold"),
        ("context_pressure_threshold", float("nan"), "context_pressure_threshold"),
        ("context_pressure_threshold", float("inf"), "context_pressure_threshold"),
        ("turn_offset", -1, "turn_offset"),
        ("turn_offset", True, "turn_offset"),
        ("tool_iteration_offset", -1, "tool_iteration_offset"),
        ("tool_iteration_offset", False, "tool_iteration_offset"),
    ],
)
def test_rejects_invalid_runtime_limits(field: str, value: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        AgentLoopConfig(
            provider=ScriptedProvider([]),
            tool_executor=NeverToolExecutor(),
            **cast(dict[str, object], {field: value}),
        )


def test_accepts_runtime_limits_at_their_bounds() -> None:
    config = AgentLoopConfig(
        provider=ScriptedProvider([]),
        tool_executor=NeverToolExecutor(),
        max_tool_iterations=0,
        context_window=1,
        context_reserve_tokens=1,
        context_pressure_threshold=1,
        turn_offset=0,
        tool_iteration_offset=0,
    )

    assert config.max_tool_iterations == 0
    assert config.context_pressure_threshold == 1
