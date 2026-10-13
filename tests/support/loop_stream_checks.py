"""Check agent-loop invariants on every loop stream a test drives.

`tests/conftest.py` wraps each `run_agent_loop` binding the code under test calls
with `checked_run_agent_loop`. When a wrapped stream finishes or raises, its events
are checked with `assert_loop_stream_invariants`, and any violation fails the test
at teardown. Streams the consumer closes early, or that are cancelled from outside,
are not checked: the loop does not publish terminals into a closed consumer or a
cancelled task.

Mark a test with ``@pytest.mark.loop_invariants_exempt(reason="...")`` when it
deliberately produces a stream that breaks these rules.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Sequence
from typing import Any

import anyio
import pytest

from tests.support.agent_runtime import assert_tool_result_pairing, assert_turn_invariants

type RunAgentLoop = Callable[..., AsyncGenerator[Any, None]]

# Modules whose `run_agent_loop` binding production code calls.
LOOP_CALLER_MODULES = ("wisp.agent.harness.runner", "wisp.coding.compaction")


def is_exempt(node: pytest.Item) -> bool:
    """Return whether a test opted out of the automatic loop-stream checks.

    Args:
        node (pytest.Item): The running test.

    Returns:
        bool: True when the test carries ``loop_invariants_exempt`` with a reason.

    Raises:
        pytest.UsageError: The marker has no non-empty ``reason``.
    """

    marker = node.get_closest_marker("loop_invariants_exempt")
    if marker is None:
        return False
    reason = marker.kwargs.get("reason", marker.args[0] if marker.args else None)
    if not isinstance(reason, str) or not reason.strip():
        raise pytest.UsageError(
            f"{node.nodeid}: loop_invariants_exempt needs a reason=... explaining the exemption"
        )
    return True


def assert_loop_stream_invariants(events: Sequence[object]) -> None:
    """Check the rules every finished agent-loop stream must satisfy.

    Args:
        events (Sequence[object]): Events the stream published, in order.

    Raises:
        AssertionError: A started turn lacks exactly one terminal, turns are out of
            order or contain events outside them, or tool results are not paired.
    """

    assert_turn_invariants(events)
    assert_tool_result_pairing(events)


def checked_run_agent_loop(
    run_agent_loop: RunAgentLoop,
    record_violation: Callable[[str], None],
) -> RunAgentLoop:
    """Wrap `run_agent_loop` so each finished stream is checked.

    Args:
        run_agent_loop (RunAgentLoop): The real loop function.
        record_violation (Callable[[str], None]): Receives a description of each
            violation; the caller decides how to fail the test.

    Returns:
        RunAgentLoop: A drop-in replacement with the same signature and events.
    """

    def run(*args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        async def checked() -> AsyncGenerator[Any, None]:
            events: list[object] = []
            stream = run_agent_loop(*args, **kwargs)
            finished = False
            try:
                async for event in stream:
                    events.append(event)
                    yield event
                finished = True
            except GeneratorExit:
                raise
            except BaseException as exc:
                finished = not isinstance(exc, anyio.get_cancelled_exc_class())
                raise
            finally:
                await stream.aclose()
                if finished:
                    try:
                        assert_loop_stream_invariants(events)
                    except AssertionError as error:
                        event_types = ", ".join(
                            str(getattr(event, "type", type(event).__name__)) for event in events
                        )
                        record_violation(f"{error}\n    events: {event_types}")

        return checked()

    return run


__all__ = [
    "LOOP_CALLER_MODULES",
    "assert_loop_stream_invariants",
    "checked_run_agent_loop",
    "is_exempt",
]
