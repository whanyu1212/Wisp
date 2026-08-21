from __future__ import annotations

import anyio
import pytest
from textual import events
from textual.containers import VerticalScroll
from textual.geometry import Region, Size
from textual.widget import Widget

from wisp.tui.textual_app import TextualTui
from wisp.tui.widgets import LineMessage, StreamMessage, Transcript

pytestmark = pytest.mark.tui


def test_stream_message_applies_measured_geometry_without_recursive_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> tuple[Size, list[bool]]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            stream = StreamMessage("one line")
            await app.query_one("#transcript", Transcript).mount(stream)
            await pilot.pause()

            layout_requests: list[bool] = []
            original_refresh = Widget.refresh

            def record_refresh(
                widget: Widget,
                *regions: Region,
                repaint: bool = True,
                layout: bool = False,
                recompose: bool = False,
            ) -> Widget:
                if widget is stream:
                    layout_requests.append(layout)
                return original_refresh(
                    widget,
                    *regions,
                    repaint=repaint,
                    layout=layout,
                    recompose=recompose,
                )

            monkeypatch.setattr(Widget, "refresh", record_refresh)
            measured_virtual_size = Size(
                stream.virtual_size.width,
                stream.virtual_size.height + 3,
            )
            changed = stream._size_updated(
                stream.size,
                measured_virtual_size,
                stream.container_size,
            )

            assert changed
            return stream.virtual_size, layout_requests

    virtual_size, layout_requests = anyio.run(scenario)

    assert virtual_size.height >= 3
    assert True not in layout_requests


def test_transcript_applies_measured_geometry_without_recursive_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegated_layout: list[bool] = []

    def record_size_update(
        _transcript: Widget,
        _size: Size,
        _virtual_size: Size,
        _container_size: Size,
        layout: bool = True,
    ) -> bool:
        delegated_layout.append(layout)
        return True

    monkeypatch.setattr(VerticalScroll, "_size_updated", record_size_update)

    changed = Transcript()._size_updated(
        Size(80, 20),
        Size(80, 40),
        Size(80, 20),
    )

    assert changed
    assert delegated_layout == [False]


def test_stream_message_still_reflows_after_terminal_resize() -> None:
    source = "A paragraph with enough words to wrap across several rows in a narrow viewport."

    async def scenario() -> tuple[int, int, str]:
        app = TextualTui()
        async with app.run_test(size=(100, 24)) as pilot:
            stream = StreamMessage(source)
            await app.query_one("#transcript", Transcript).mount(stream)
            await pilot.pause()
            wide_height = stream.content_size.height

            await pilot.resize_terminal(32, 24)
            await pilot.pause()
            await pilot.pause()
            return wide_height, stream.content_size.height, stream.source

    wide_height, narrow_height, retained_source = anyio.run(scenario)

    assert narrow_height > wide_height
    assert retained_source == source


def test_shrinking_content_does_not_leave_an_unreachable_scroll_target() -> None:
    """Wheel scrolling must keep working after content shrinks under the reader.

    Textual steps the wheel from ``scroll_target_y``, but re-validates only
    ``scroll_y`` when content shrinks. A target stranded above the new
    ``max_scroll_y`` silently absorbs every wheel event -- each subtracts a step,
    clamps back to the same row, and reports that nothing scrolled -- so the
    transcript freezes until the overshoot is spent.
    """

    async def scenario() -> tuple[float, int, float, float]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            transcript = app.query_one("#transcript", Transcript)
            filler = [LineMessage(f"line {index}", role="assistant") for index in range(80)]
            for message in filler:
                await transcript.mount(message)
            surplus = [LineMessage(f"surplus {index}", role="assistant") for index in range(80)]
            for message in surplus:
                await transcript.mount(message)
            await pilot.pause()

            transcript.scroll_end(animate=False)
            await pilot.pause()
            parked_target = transcript.scroll_target_y

            # Collapse the content under the parked viewport, as a process card does
            # when it replaces captured output with a bounded preview.
            for message in surplus:
                message.remove()
            await pilot.pause()

            before_scroll_y = transcript.scroll_y
            await pilot._post_mouse_events([events.MouseScrollUp], widget=transcript, times=1)
            for _ in range(20):
                await pilot.pause()
                if transcript.scroll_y != before_scroll_y:
                    break
            return (
                parked_target,
                transcript.max_scroll_y,
                transcript.scroll_target_y,
                before_scroll_y - transcript.scroll_y,
            )

    parked_target, max_scroll_y, scroll_target_y, rows_scrolled = anyio.run(scenario)

    # The fixture must actually strand the target, or this proves nothing.
    assert parked_target > max_scroll_y
    # The stranded target is discarded, so the very next wheel event scrolls.
    assert scroll_target_y <= max_scroll_y
    assert rows_scrolled > 0
