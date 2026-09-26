from __future__ import annotations

from typing import cast

import pytest

from tests.agent.harness.support import (
    RecordingToolExecutor,
)
from wisp.agent.harness import AgentHarnessConfig
from wisp.providers.fake import ScriptedProvider


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
