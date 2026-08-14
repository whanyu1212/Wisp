from __future__ import annotations

from typing import Any, cast

import pytest

from wisp.tui import stream_buffer
from wisp.tui.stream_buffer import (
    MarkdownStreamController,
    _next_drain_delay,
    _StreamTurn,
)
from wisp.tui.widgets import StreamMessage

pytestmark = pytest.mark.tui


class _ScheduledTimer:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _Loop:
    def __init__(self) -> None:
        self.delays: list[float] = []
        self.timer = _ScheduledTimer()

    def call_later(self, delay: float, _callback: object, _turn: object) -> _ScheduledTimer:
        self.delays.append(delay)
        return self.timer


class _App:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def call_after_refresh(self, callback: object, _turn: object) -> bool:
        self.callbacks.append(callback)
        return True


def _turn() -> _StreamTurn:
    return _StreamTurn(
        widget=StreamMessage(),
        mounted=cast(Any, object()),
    )


@pytest.mark.parametrize(
    ("render_seconds", "expected"),
    [
        (None, 1 / 15),
        (0.01, 1 / 15),
        (0.1, 0.2),
        (1.0, 0.25),
    ],
)
def test_next_drain_delay_is_cost_aware_and_bounded(
    render_seconds: float | None,
    expected: float,
) -> None:
    assert _next_drain_delay(render_seconds) == pytest.approx(expected)


def test_first_stream_write_is_scheduled_immediately() -> None:
    app = _App()
    controller = MarkdownStreamController(cast(Any, app))
    turn = _turn()

    controller._queue_drain(turn, immediate=True)

    assert app.callbacks == [controller._drain]
    assert turn.drain_scheduled is True
    assert controller._pending_callbacks == 1


def test_large_pending_burst_respects_render_cost_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _App()
    loop = _Loop()
    controller = MarkdownStreamController(cast(Any, app))
    turn = _turn()
    turn.last_render_seconds = 0.1
    turn.pending_bytes = controller._DRAIN_IMMEDIATE_BYTES

    monkeypatch.setattr(stream_buffer.asyncio, "get_running_loop", lambda: loop)

    controller._queue_drain(turn)

    assert app.callbacks == []
    assert loop.delays == [pytest.approx(0.2)]
    assert turn.drain_timer is loop.timer


def test_flush_cancels_backoff_and_schedules_immediate_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _App()
    loop = _Loop()
    controller = MarkdownStreamController(cast(Any, app))
    turn = _turn()
    turn.last_render_seconds = 0.1
    turn.pending.append("pending")
    turn.pending_bytes = len("pending")
    controller._turn = turn

    monkeypatch.setattr(stream_buffer.asyncio, "get_running_loop", lambda: loop)

    controller._queue_drain(turn)
    controller.flush("authoritative")

    assert loop.timer.cancelled is True
    assert controller._turn is None
    assert turn.finalize_requested is True
    assert app.callbacks == [controller._finalize]
    assert controller._pending_callbacks == 1
