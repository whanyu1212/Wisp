"""Deliver a running tool's progress to frontends without letting them slow it down.

A tool reports progress into a one-slot buffer that always holds only the newest
snapshot. A forwarder task sends that snapshot to the frontend when the frontend
is ready, at most every ``_MIN_INTERVAL_SECONDS`` and never before
``_FIRST_DELAY_SECONDS``. A slow frontend therefore sees fewer snapshots; the tool
never waits for it and nothing queues up.

``run_with_progress`` owns the forwarder for exactly one call. It stops the
forwarder, and waits for a delivery already in flight, before it returns, so no
progress event can follow the call's ``tool.execution.ended``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Final

import anyio

from wisp.events import (
    TOOL_PROGRESS_TAIL_MAX_BYTES,
    TOOL_PROGRESS_TAIL_MAX_LINES,
    ToolExecutionProgress,
    WispEvent,
)
from wisp.tools.progress import OutputProgress, ToolProgress

_logger = logging.getLogger(__name__)

type LiveEventSink = Callable[[WispEvent], Awaitable[None]]
"""Delivers a transient event to frontends; it may wait while they catch up."""

# Quick calls finish before the first update, so they publish no progress at all.
_FIRST_DELAY_SECONDS: Final = 0.5
_MIN_INTERVAL_SECONDS: Final = 0.25


class _LatestProgress:
    """The newest progress of one call, replaced by every report."""

    def __init__(self) -> None:
        self.latest: OutputProgress | None = None
        self.closed = False
        self.changed = anyio.Event()
        self.closed_event = anyio.Event()

    def report(self, progress: OutputProgress) -> None:
        if self.closed:
            return
        self.latest = progress
        self.changed.set()

    def take(self) -> OutputProgress | None:
        progress, self.latest = self.latest, None
        self.changed = anyio.Event()
        return progress

    def close(self) -> None:
        self.closed = True
        self.changed.set()
        self.closed_event.set()


async def run_with_progress[T](
    sink: LiveEventSink,
    *,
    call_id: str,
    name: str,
    run: Callable[[ToolProgress], Awaitable[T]],
) -> T:
    """Run one tool call while forwarding the progress it reports.

    Args:
        sink (LiveEventSink): Where progress events go.
        call_id (str): The call the progress belongs to.
        name (str): The tool's name.
        run (Callable[[ToolProgress], Awaitable[T]]): Runs the call, reporting
            progress to the handle it is given.

    Returns:
        T: What ``run`` returned. By then the forwarder has stopped, so no
            progress for this call is published afterwards.
    """

    slot = _LatestProgress()
    started = time.monotonic()

    async def forward() -> None:
        sequence = 0
        next_send = started + _FIRST_DELAY_SECONDS
        while True:
            await slot.changed.wait()
            if slot.closed:
                return
            # Throttle, but let the call's end interrupt the wait so it never
            # delays the result.
            with anyio.move_on_after(max(0.0, next_send - time.monotonic())):
                await slot.closed_event.wait()
            if slot.closed:
                return
            progress = slot.take()
            if progress is None:
                continue
            sequence += 1
            event = _progress_event(
                progress,
                call_id=call_id,
                name=name,
                sequence=sequence,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            try:
                await sink(event)
            except Exception:
                # Progress is optional. A frontend that cannot take it fails on the
                # run's own events; the tool keeps running either way.
                _logger.debug("Stopped forwarding progress for %s", call_id, exc_info=True)
                return
            next_send = time.monotonic() + _MIN_INTERVAL_SECONDS

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(forward)
        try:
            result = await run(slot)
        finally:
            slot.close()
    return result


def _progress_event(
    progress: OutputProgress,
    *,
    call_id: str,
    name: str,
    sequence: int,
    elapsed_ms: int,
) -> ToolExecutionProgress:
    return ToolExecutionProgress(
        call_id=call_id,
        name=name,
        sequence=sequence,
        elapsed_ms=elapsed_ms,
        stdout_tail=bounded_tail(progress.stdout_tail),
        stderr_tail=bounded_tail(progress.stderr_tail),
        stdout_bytes=max(0, progress.stdout_bytes),
        stderr_bytes=max(0, progress.stderr_bytes),
    )


def bounded_tail(text: str) -> str:
    """Return the end of ``text`` within the progress tail's line and byte limits.

    Args:
        text (str): Output so far.

    Returns:
        str: At most the last 20 lines and 4,096 UTF-8 bytes, starting on a
            character boundary.
    """

    lines = text.splitlines(keepends=True)
    if len(lines) > TOOL_PROGRESS_TAIL_MAX_LINES:
        text = "".join(lines[-TOOL_PROGRESS_TAIL_MAX_LINES:])
    encoded = text.encode("utf-8")
    if len(encoded) <= TOOL_PROGRESS_TAIL_MAX_BYTES:
        return text
    return encoded[-TOOL_PROGRESS_TAIL_MAX_BYTES:].decode("utf-8", errors="ignore")
