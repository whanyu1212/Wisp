"""Bounded JSONL-RPC streaming output."""

from __future__ import annotations

from collections.abc import Callable

import anyio
from anyio.abc import TaskGroup

from wisp.events import MessageDelta, WispEvent

RPC_DELTA_FLUSH_SECONDS = 0.008
RPC_DELTA_MAX_BYTES = 8 * 1024
RPC_DELTA_MAX_PARTS = 256


class RpcEventWriter:
    """Write ordered RPC events while batching adjacent streamed text.

    The host calls this writer only after it has serialized event order. Keeping
    the pending delta here lets timer flushes preserve that order: any following
    event synchronously flushes pending text before writing itself. The agent
    loop, in-process SDK, and ordinary JSON output retain their event granularity.

    Args:
        write_event: Immediate event sink for encoded JSONL output.
        task_group: Event-loop task group that owns bounded flush timers.
        flush_seconds: Maximum time to retain the first delta in a batch.
        max_bytes: Maximum UTF-8 payload bytes retained in one batch.
        max_parts: Maximum source deltas retained in one batch.
    """

    def __init__(
        self,
        write_event: Callable[[WispEvent], None],
        task_group: TaskGroup,
        *,
        flush_seconds: float = RPC_DELTA_FLUSH_SECONDS,
        max_bytes: int = RPC_DELTA_MAX_BYTES,
        max_parts: int = RPC_DELTA_MAX_PARTS,
    ) -> None:
        self._write_event = write_event
        self._task_group = task_group
        self._flush_seconds = flush_seconds
        self._max_bytes = max_bytes
        self._max_parts = max_parts
        self._pending: MessageDelta | None = None
        self._pending_parts: list[str] = []
        self._pending_bytes = 0
        self._pending_deadline: float | None = None
        self._closed = False
        self._timer_wakeup = anyio.Event()
        self._task_group.start_soon(self._run_flush_timer)

    def __call__(self, event: WispEvent) -> None:
        """Accept one event in its already-serialized output order."""

        if self._closed:
            raise RuntimeError("RPC event writer is closed")
        if not isinstance(event, MessageDelta):
            self.flush()
            self._write_event(event)
            return

        event_bytes = len(event.delta.encode("utf-8"))
        if self._pending is None or not _compatible_message_delta(self._pending, event):
            self.flush()
            self._start_batch(event, event_bytes)
            return
        if (
            self._pending_bytes + event_bytes > self._max_bytes
            or len(self._pending_parts) >= self._max_parts
        ):
            self.flush()
            self._start_batch(event, event_bytes)
            return

        self._pending_parts.append(event.delta)
        self._pending_bytes += event_bytes
        if self._batch_is_full():
            self.flush()

    def flush(self) -> None:
        """Write the pending batch immediately, if one exists."""

        pending = self._pending
        if pending is None:
            return
        parts = self._pending_parts
        self._pending = None
        self._pending_parts = []
        self._pending_bytes = 0
        self._pending_deadline = None
        event = pending if len(parts) == 1 else pending.model_copy(update={"delta": "".join(parts)})
        self._write_event(event)

    def close(self) -> None:
        """Flush the final partial batch and reject later events."""

        if self._closed:
            return
        self.flush()
        self._closed = True
        self._timer_wakeup.set()

    def _start_batch(self, event: MessageDelta, event_bytes: int) -> None:
        self._pending = event
        self._pending_parts = [event.delta]
        self._pending_bytes = event_bytes
        self._pending_deadline = anyio.current_time() + self._flush_seconds
        if self._batch_is_full():
            self.flush()
            return
        self._timer_wakeup.set()

    def _batch_is_full(self) -> bool:
        return self._pending_bytes >= self._max_bytes or len(self._pending_parts) >= self._max_parts

    async def _run_flush_timer(self) -> None:
        """Flush the current batch through one reusable timer task."""

        while not self._closed:
            wakeup = self._timer_wakeup
            deadline = self._pending_deadline
            if deadline is None:
                await wakeup.wait()
                if wakeup is self._timer_wakeup:
                    self._timer_wakeup = anyio.Event()
                continue
            remaining = max(0.0, deadline - anyio.current_time())
            with anyio.move_on_after(remaining) as scope:
                await wakeup.wait()
            if self._closed:
                return
            if not scope.cancel_called:
                if wakeup is self._timer_wakeup:
                    self._timer_wakeup = anyio.Event()
            elif self._pending_deadline == deadline:
                self.flush()


def _compatible_message_delta(left: MessageDelta, right: MessageDelta) -> bool:
    """Return whether two adjacent deltas describe the same content stream."""

    return (
        left.turn == right.turn
        and left.role == right.role
        and left.content_index == right.content_index
        and left.content_kind == right.content_kind
    )


__all__ = [
    "RPC_DELTA_FLUSH_SECONDS",
    "RPC_DELTA_MAX_BYTES",
    "RPC_DELTA_MAX_PARTS",
    "RpcEventWriter",
]
