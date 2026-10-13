"""Tests for the automatic agent-loop stream checks in ``tests/conftest.py``."""

from __future__ import annotations

import sys
from collections.abc import AsyncGenerator
from typing import Any

import anyio
import pytest

import wisp.agent.harness.runner as harness_runner
import wisp.agent.loop.runner as loop_runner
from tests.agent.invariant_checks_support import completed_turn
from tests.support.loop_stream_checks import checked_run_agent_loop, is_exempt
from wisp.events import TurnStarted


def _scripted_loop(events: tuple[object, ...], *, then_raise: Exception | None = None) -> Any:
    async def run(*_args: object, **_kwargs: object) -> AsyncGenerator[object, None]:
        for event in events:
            yield event
        if then_raise is not None:
            raise then_raise

    return run


def _consume(run: Any) -> None:
    async def main() -> None:
        async for _event in run():
            pass

    anyio.run(main)


def test_finished_stream_with_valid_turns_passes() -> None:
    violations: list[str] = []

    _consume(checked_run_agent_loop(_scripted_loop(completed_turn()), violations.append))

    assert violations == []


def test_finished_stream_with_open_turn_is_reported() -> None:
    violations: list[str] = []

    _consume(checked_run_agent_loop(_scripted_loop((TurnStarted(turn=1),)), violations.append))

    assert len(violations) == 1
    assert "turn.started" in violations[0]


def test_raising_stream_is_checked_and_still_raises() -> None:
    violations: list[str] = []
    run = checked_run_agent_loop(
        _scripted_loop((TurnStarted(turn=1),), then_raise=RuntimeError("provider failed")),
        violations.append,
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        _consume(run)

    assert len(violations) == 1


def test_stream_closed_by_consumer_is_not_checked() -> None:
    violations: list[str] = []
    run = checked_run_agent_loop(_scripted_loop(completed_turn()), violations.append)

    async def main() -> None:
        stream = run()
        await stream.__anext__()
        await stream.aclose()

    anyio.run(main)

    assert violations == []


def test_cancelled_stream_is_not_checked() -> None:
    violations: list[str] = []

    async def blocking_loop(*_args: object, **_kwargs: object) -> AsyncGenerator[object, None]:
        yield TurnStarted(turn=1)
        await anyio.sleep_forever()
        yield TurnStarted(turn=2)  # pragma: no cover

    run = checked_run_agent_loop(blocking_loop, violations.append)

    async def main() -> None:
        with anyio.move_on_after(0.01):
            async for _event in run():
                pass

    anyio.run(main)

    assert violations == []


def test_fixture_wraps_the_loop_callers() -> None:
    """Production callers of the loop get the checked wrapper during a test."""

    real = sys.modules["wisp.agent.loop.runner"].run_agent_loop

    assert harness_runner.run_agent_loop is not real
    assert loop_runner.run_agent_loop is real


@pytest.mark.loop_invariants_exempt(reason="checks that an exempt test is left unwrapped")
def test_exempt_test_keeps_the_real_loop() -> None:
    assert harness_runner.run_agent_loop is loop_runner.run_agent_loop


@pytest.mark.parametrize(
    "marker",
    [
        pytest.mark.loop_invariants_exempt(),
        pytest.mark.loop_invariants_exempt(reason="  "),
    ],
)
def test_exemption_requires_a_reason(marker: pytest.MarkDecorator) -> None:
    class Node:
        nodeid = "tests/example.py::test_example"

        def get_closest_marker(self, name: str) -> pytest.Mark | None:
            return marker.mark if name == marker.mark.name else None

    with pytest.raises(pytest.UsageError, match="needs a reason"):
        is_exempt(Node())  # type: ignore[arg-type]
