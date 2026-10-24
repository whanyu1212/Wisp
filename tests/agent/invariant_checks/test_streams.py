"""Tests for the automatic agent-loop stream checks in ``tests/conftest.py``."""

from __future__ import annotations

import types
from collections.abc import AsyncGenerator
from typing import Any

import anyio
import pytest

import tests.support.loop_stream_checks as loop_stream_checks
import wisp.agent.harness.runner as harness_runner
import wisp.agent.loop as loop_package
import wisp.agent.loop.runner as loop_runner
import wisp.coding.compaction as compaction_module
from tests.agent.invariant_checks.support import completed_turn
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

    _consume(checked_run_agent_loop(_scripted_loop(completed_turn()), lambda: violations.append))

    assert violations == []


def test_finished_stream_open_turn_reported() -> None:
    violations: list[str] = []

    _consume(
        checked_run_agent_loop(_scripted_loop((TurnStarted(turn=1),)), lambda: violations.append)
    )

    assert len(violations) == 1
    assert "turn.started" in violations[0]


def test_raising_stream_checked_and_reraised() -> None:
    violations: list[str] = []
    run = checked_run_agent_loop(
        _scripted_loop((TurnStarted(turn=1),), then_raise=RuntimeError("provider failed")),
        lambda: violations.append,
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        _consume(run)

    assert len(violations) == 1


def test_consumer_closed_stream_not_checked() -> None:
    violations: list[str] = []
    run = checked_run_agent_loop(_scripted_loop(completed_turn()), lambda: violations.append)

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

    run = checked_run_agent_loop(blocking_loop, lambda: violations.append)

    async def main() -> None:
        with anyio.move_on_after(0.01):
            async for _event in run():
                pass

    anyio.run(main)

    assert violations == []


def test_stream_without_sink_passes_unchecked() -> None:
    """An exempt test's streams are delivered unchanged and never reported."""

    started = TurnStarted(turn=1)
    run = checked_run_agent_loop(_scripted_loop((started,)), lambda: None)
    received: list[object] = []

    async def main() -> None:
        async for event in run():
            received.append(event)

    anyio.run(main)

    assert received == [started]


def test_loop_checked_at_source() -> None:
    """Every caller binds the checked loop, whenever it was imported."""

    checked = loop_runner.run_agent_loop
    assert getattr(checked, "__wrapped__", None) is not None
    assert loop_package.run_agent_loop is checked
    assert harness_runner.run_agent_loop is checked
    assert compaction_module.run_agent_loop is checked


def test_late_import_binds_checked_loop() -> None:
    """A late import (in a fixture or test body) cannot bypass the checks."""

    late_module = types.ModuleType("late_loop_caller")
    exec("from wisp.agent.loop import run_agent_loop", late_module.__dict__)

    assert late_module.run_agent_loop is loop_runner.run_agent_loop
    assert getattr(late_module.run_agent_loop, "__wrapped__", None) is not None


def test_checks_route_to_running_test() -> None:
    assert loop_stream_checks._active_sink is not None


@pytest.mark.loop_invariants_exempt(reason="checks that an exempt test is not reported")
def test_exempt_test_lacks_violation_sink() -> None:
    assert loop_stream_checks._active_sink is None


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
