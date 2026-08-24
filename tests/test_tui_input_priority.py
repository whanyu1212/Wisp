from __future__ import annotations

import anyio
import pytest
from rich.segment import Segment
from textual import events
from textual._compositor import ChopsUpdate
from textual.app import App
from textual.strip import Strip

from wisp.tui.input_priority import InputPriorityPolicy
from wisp.tui.textual_app import TextualTui, _DisplayedFrame

pytestmark = pytest.mark.tui


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_input_priority_closes_on_first_emitted_frame() -> None:
    clock = _Clock()
    policy = InputPriorityPolicy(clock=clock)
    token = ("typing", 1.0)

    assert policy.observe_input(token)
    assert not policy.observe_input(token)
    assert policy.pending_count == 1
    assert policy.drain_delay(None) == pytest.approx((0.1, 0.0))

    clock.now = 0.02
    assert policy.frame_emitted()
    assert policy.pending_count == 0
    assert policy.drain_delay(None) == (0.0, None)
    assert not policy.frame_emitted()


def test_later_input_cannot_extend_an_existing_drain_past_its_cap() -> None:
    clock = _Clock()
    policy = InputPriorityPolicy(clock=clock)
    assert policy.observe_input(("typing", 1.0))
    delay, started_at = policy.drain_delay(None)
    assert delay == pytest.approx(0.1)
    assert started_at == 0.0

    clock.now = 0.05
    assert policy.observe_input(("typing", 2.0))
    delay, retained_start = policy.drain_delay(started_at)
    assert delay == pytest.approx(0.05)
    assert retained_start == started_at

    clock.now = 0.1
    assert policy.drain_delay(started_at) == (0.0, None)
    assert policy.pending_count == 2


def test_input_priority_expires_and_failed_input_can_be_cancelled() -> None:
    clock = _Clock()
    policy = InputPriorityPolicy(clock=clock)
    cancelled = ("submission", 1.0)
    expiring = ("cancellation", 2.0)

    assert policy.observe_input(cancelled)
    policy.cancel_input(cancelled)
    assert policy.pending_count == 0

    assert policy.observe_input(expiring)
    clock.now = 0.1
    assert policy.pending_count == 0
    assert policy.drain_delay(None) == (0.0, None)


def test_input_priority_rejects_non_positive_deferral() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        InputPriorityPolicy(max_deferral_seconds=0)


def test_textual_tracks_priority_without_enabling_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = TextualTui()

    async def discard_event(_app: App[object], _event: events.Event) -> None:
        return

    monkeypatch.setattr(App, "on_event", discard_event)
    monkeypatch.setattr(app, "_input_event_category", lambda _event: "typing")

    anyio.run(app.on_event, events.Key("x", "x"))

    assert app._diagnostics is None
    assert app._pending_input_latency == []
    assert app._input_priority.pending_count == 1
    app._input_frame_emitted(0.0)
    assert app._input_priority.pending_count == 0


def test_filtered_duplicate_frame_does_not_release_input_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resumed: list[bool] = []
    app = TextualTui()

    def discard_display(
        _app: App[object],
        _screen: object,
        _renderable: object,
    ) -> None:
        return

    monkeypatch.setattr(App, "_display", discard_display)
    monkeypatch.setattr(
        app._stream,
        "resume_after_input_frame",
        lambda: resumed.append(True),
    )

    async def scenario() -> None:
        async with app.run_test():
            screen = app.screen
            size = screen.outer_size
            blank = Strip([Segment(" " * size.width)], size.width)
            app._displayed_screen = screen
            app._displayed_cursor_position = size.clamp_offset(app.cursor_position)
            app._displayed_frame = _DisplayedFrame(
                size=size,
                rows=[blank for _ in range(size.height)],
            )
            duplicate = ChopsUpdate(
                [{0: blank}, *({} for _ in range(size.height - 1))],
                [(0, 0, size.width)],
                [[size.width], *([] for _ in range(size.height - 1))],
            )
            app._input_priority.observe_input(("typing", 1.0))
            app._display(screen, duplicate)
            assert app._input_priority.pending_count == 1
            assert resumed == []

            changed = Strip([Segment("x" + " " * (size.width - 1))], size.width)
            update = ChopsUpdate(
                [{0: changed}, *({} for _ in range(size.height - 1))],
                [(0, 0, size.width)],
                [[size.width], *([] for _ in range(size.height - 1))],
            )
            app._display(screen, update)

    anyio.run(scenario)

    assert app._input_priority.pending_count == 0
    assert resumed == [True]
