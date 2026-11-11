from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

import anyio
import pytest

from wisp.coding import tool_progress as tool_progress_module
from wisp.coding.tool_progress import bounded_tail, run_with_progress
from wisp.events import ToolExecutionProgress, WispEvent
from wisp.tools.progress import OutputProgress, ToolProgress

T = TypeVar("T")


def output(stdout: str) -> OutputProgress:
    return OutputProgress(
        stdout_tail=stdout, stderr_tail="", stdout_bytes=len(stdout), stderr_bytes=0
    )


@pytest.fixture
def no_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_progress_module, "_FIRST_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(tool_progress_module, "_MIN_INTERVAL_SECONDS", 0.0)


class RecordingSink:
    """Records delivered progress; optionally holds each delivery until released."""

    def __init__(self, *, hold: bool = False) -> None:
        self.events: list[ToolExecutionProgress] = []
        self.hold = hold
        self.delivering = anyio.Event()
        self.release = anyio.Event()
        self.delivered = anyio.Event()

    async def __call__(self, event: WispEvent) -> None:
        assert isinstance(event, ToolExecutionProgress)
        self.delivering.set()
        if self.hold:
            await self.release.wait()
        self.events.append(event)
        self.delivered.set()

    def tails(self) -> list[str]:
        return [event.stdout_tail for event in self.events]


def run(scenario: Callable[[], Awaitable[T]]) -> T:
    return anyio.run(scenario)


def test_a_call_that_ends_before_the_first_delay_publishes_nothing() -> None:
    sink = RecordingSink()

    async def tool(progress: ToolProgress) -> str:
        progress.report(output("quick\n"))
        return "result"

    async def scenario() -> str:
        return await run_with_progress(sink, call_id="call-1", name="bash", run=tool)

    assert run(scenario) == "result"
    assert sink.events == []


def test_first_progress_waits_for_the_first_delay() -> None:
    sink = RecordingSink()

    async def tool(progress: ToolProgress) -> None:
        progress.report(output("early\n"))
        await anyio.sleep(0.3)
        # Still inside the first half second: nothing may have been sent yet.
        assert sink.events == []
        await sink.delivered.wait()

    async def scenario() -> None:
        await run_with_progress(sink, call_id="call-1", name="bash", run=tool)

    run(scenario)

    assert sink.tails() == ["early\n"]
    assert 500 <= sink.events[0].elapsed_ms < 2_000


@pytest.mark.usefixtures("no_throttle")
def test_negative_byte_counts_from_a_tool_are_clamped() -> None:
    sink = RecordingSink()

    async def tool(progress: ToolProgress) -> None:
        progress.report(
            OutputProgress(stdout_tail="", stderr_tail="", stdout_bytes=-5, stderr_bytes=-1)
        )
        await sink.delivered.wait()

    async def scenario() -> None:
        await run_with_progress(sink, call_id="call-1", name="bash", run=tool)

    run(scenario)

    assert (sink.events[0].stdout_bytes, sink.events[0].stderr_bytes) == (0, 0)


@pytest.mark.usefixtures("no_throttle")
def test_newest_report_replaces_what_a_busy_frontend_has_not_taken() -> None:
    sink = RecordingSink(hold=True)

    async def tool(progress: ToolProgress) -> None:
        progress.report(output("first\n"))
        await sink.delivering.wait()
        # The frontend is still taking "first"; these pile into one slot.
        progress.report(output("second\n"))
        progress.report(output("third\n"))
        sink.delivered = anyio.Event()
        sink.release.set()
        await sink.delivered.wait()
        sink.delivered = anyio.Event()
        await sink.delivered.wait()

    async def scenario() -> None:
        await run_with_progress(sink, call_id="call-1", name="bash", run=tool)

    run(scenario)

    assert sink.tails() == ["first\n", "third\n"]
    assert [event.sequence for event in sink.events] == [1, 2]
    assert {(event.call_id, event.name) for event in sink.events} == {("call-1", "bash")}


@pytest.mark.usefixtures("no_throttle")
def test_returns_only_after_an_in_flight_delivery_and_ignores_later_reports() -> None:
    sink = RecordingSink(hold=True)
    order: list[str] = []
    handle: list[ToolProgress] = []

    async def tool(progress: ToolProgress) -> None:
        handle.append(progress)
        progress.report(output("running\n"))
        await sink.delivering.wait()

    async def release_later() -> None:
        await sink.delivering.wait()
        await anyio.sleep(0.05)
        order.append("delivered")
        sink.release.set()

    async def scenario() -> None:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(release_later)
            await run_with_progress(sink, call_id="call-1", name="bash", run=tool)
            order.append("returned")
        handle[0].report(output("after the end\n"))
        await anyio.sleep(0.05)

    run(scenario)

    assert order == ["delivered", "returned"]
    assert sink.tails() == ["running\n"]


@pytest.mark.usefixtures("no_throttle")
def test_a_failing_frontend_stops_progress_but_not_the_tool() -> None:
    calls: list[WispEvent] = []
    failed = anyio.Event()

    async def failing_sink(event: WispEvent) -> None:
        calls.append(event)
        failed.set()
        raise BrokenPipeError("frontend went away")

    async def tool(progress: ToolProgress) -> str:
        progress.report(output("one\n"))
        await failed.wait()
        progress.report(output("two\n"))
        await anyio.sleep(0.05)
        return "finished"

    async def scenario() -> str:
        return await run_with_progress(failing_sink, call_id="call-1", name="bash", run=tool)

    assert run(scenario) == "finished"
    assert len(calls) == 1


@pytest.mark.usefixtures("no_throttle")
def test_cancelling_the_call_stops_progress() -> None:
    sink = RecordingSink()
    handle: list[ToolProgress] = []

    async def scenario() -> bool:
        with anyio.CancelScope() as scope:

            async def tool(progress: ToolProgress) -> None:
                handle.append(progress)
                progress.report(output("before\n"))
                await sink.delivered.wait()
                scope.cancel()
                await anyio.sleep_forever()

            await run_with_progress(sink, call_id="call-1", name="bash", run=tool)
        handle[0].report(output("after\n"))
        await anyio.sleep(0.05)
        return scope.cancelled_caught

    assert run(scenario) is True
    assert sink.tails() == ["before\n"]


def test_throttles_delivery_to_the_minimum_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sends stay at least the interval apart, however long the tool takes to run.

    The bound on the number of sends comes from the tool's measured duration, not
    from the duration it would have on an idle machine: under parallel load the
    fifty short sleeps take longer, and the forwarder rightly sends more often.
    """

    monkeypatch.setattr(tool_progress_module, "_FIRST_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(tool_progress_module, "_MIN_INTERVAL_SECONDS", 0.2)
    sink = RecordingSink()

    async def tool(progress: ToolProgress) -> None:
        for index in range(50):
            progress.report(output(f"line {index}\n"))
            await anyio.sleep(0.01)

    async def scenario() -> float:
        started = time.monotonic()
        await run_with_progress(sink, call_id="call-1", name="bash", run=tool)
        return time.monotonic() - started

    duration = run(scenario)

    # Fifty reports without the throttle would mean dozens of sends.
    assert 1 <= len(sink.events) <= math.floor(duration / 0.2) + 1
    elapsed = [event.elapsed_ms for event in sink.events]
    assert all(later - earlier >= 190 for earlier, later in zip(elapsed, elapsed[1:], strict=False))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("short\n", "short\n"),
        ("".join(f"{n}\n" for n in range(20)), "".join(f"{n}\n" for n in range(20))),
        ("x" * 4096, "x" * 4096),
        ("x" * 4097, "x" * 4096),
        ("".join(f"{n}\n" for n in range(30)), "".join(f"{n}\n" for n in range(10, 30))),
        ("é" * 3000, "é" * 2048),
    ],
    ids=[
        "fits",
        "exactly-20-lines",
        "exactly-4096-bytes",
        "one-byte-over",
        "keeps-last-20-lines",
        "keeps-last-4096-bytes-on-a-character",
    ],
)
def test_bounded_tail(text: str, expected: str) -> None:
    assert bounded_tail(text) == expected


@pytest.mark.parametrize("error", [ValueError("bad input"), KeyError("missing")])
def test_a_tool_error_escapes_unwrapped(error: Exception) -> None:
    # Callers recognise tool failures by type, so the task group that runs the
    # forwarder must not wrap the tool's own exception in an ExceptionGroup.
    sink = RecordingSink()

    async def tool(progress: ToolProgress) -> None:
        raise error

    async def scenario() -> None:
        await run_with_progress(sink, call_id="call-1", name="bash", run=tool)

    with pytest.raises(type(error)) as caught:
        run(scenario)
    assert caught.value is error
