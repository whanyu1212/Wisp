"""Check agent-loop invariants on every loop stream a test drives.

At session start, `install_loop_stream_checks` replaces `run_agent_loop` at its
source (`wisp.agent.loop.runner` and the `wisp.agent.loop` package), so every
caller binds the checked version whenever it is imported: the harness, the
compaction summarizer, and tests that import the loop directly. For each test,
`collecting` routes violations to that test, which `tests/conftest.py` fails at
teardown.

A stream is checked with `assert_loop_stream_invariants` when it finishes or
raises. Streams the consumer closes early, or that are cancelled from outside,
are not checked: the loop does not publish terminals into a closed consumer or a
cancelled task.

Mark a test with ``@pytest.mark.loop_invariants_exempt(reason="...")`` when it
deliberately produces a stream that breaks these rules.
"""

from __future__ import annotations

import functools
from collections.abc import AsyncGenerator, Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import anyio
import pytest

from tests.support.agent_runtime import (
    assert_message_pairing,
    assert_tool_result_pairing,
    assert_turn_invariants,
)

type RunAgentLoop = Callable[..., AsyncGenerator[Any, None]]
type ViolationSink = Callable[[str], None]

# Where the running test's violations go; None while no test, or an exempt test, runs.
_active_sink: ViolationSink | None = None


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
            order or contain events outside them, tool results are not paired, or
            an opened message does not close exactly once before its turn ends.
    """

    assert_turn_invariants(events)
    assert_tool_result_pairing(events)
    assert_message_pairing(events)


def checked_run_agent_loop(
    run_agent_loop: RunAgentLoop,
    current_sink: Callable[[], ViolationSink | None],
) -> RunAgentLoop:
    """Wrap `run_agent_loop` so each finished stream is checked.

    Args:
        run_agent_loop (RunAgentLoop): The real loop function.
        current_sink (Callable[[], ViolationSink | None]): Returns where to report
            violations when a stream starts; None passes the stream through unchecked.

    Returns:
        RunAgentLoop: An async generator function with the same signature and events.
    """

    @functools.wraps(run_agent_loop)
    async def checked(*args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        sink = current_sink()
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
            if finished and sink is not None:
                try:
                    assert_loop_stream_invariants(events)
                except AssertionError as error:
                    event_types = ", ".join(
                        str(getattr(event, "type", type(event).__name__)) for event in events
                    )
                    sink(f"{error}\n    events: {event_types}")

    return checked


def install_loop_stream_checks() -> None:
    """Replace `run_agent_loop` at its source with the checked version, once.

    Later imports of the loop, the harness, or the compaction summarizer bind the
    checked function, so coverage does not depend on import timing.
    """

    import wisp.agent.loop as loop_package
    import wisp.agent.loop.runner as loop_runner

    if getattr(loop_runner.run_agent_loop, "__wrapped__", None) is not None:
        return
    checked = checked_run_agent_loop(loop_runner.run_agent_loop, lambda: _active_sink)
    loop_runner.run_agent_loop = checked
    loop_package.run_agent_loop = checked


@contextmanager
def collecting(sink: ViolationSink | None) -> Iterator[None]:
    """Route loop-stream violations to one test while it runs.

    Args:
        sink (ViolationSink | None): Receives each violation, or None to skip checks.

    Yields:
        None: Control while the test runs.
    """

    global _active_sink
    previous = _active_sink
    _active_sink = sink
    try:
        yield
    finally:
        _active_sink = previous


__all__ = [
    "assert_loop_stream_invariants",
    "checked_run_agent_loop",
    "collecting",
    "install_loop_stream_checks",
    "is_exempt",
]
