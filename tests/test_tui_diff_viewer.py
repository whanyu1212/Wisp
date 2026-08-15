"""End-to-end coverage for the focused full-diff reader."""

from __future__ import annotations

import anyio
import pytest
from textual import events
from textual.widgets import Static

from wisp.tui.diff_presentation import DiffOperation, DiffPresentation, DiffRow, DiffRowKind
from wisp.tui.diff_viewer import DiffViewer
from wisp.tui.textual_app import TextualTui
from wisp.tui.widgets import PromptEditor, Transcript

pytestmark = pytest.mark.tui


def _presentation() -> DiffPresentation:
    return DiffPresentation(
        path="src/example.py",
        operation=DiffOperation.modify,
        additions=80,
        deletions=0,
        rows=tuple(
            DiffRow(DiffRowKind.addition, f"line {index}", new_line=index + 1)
            for index in range(80)
        ),
        show_line_numbers=True,
    )


def test_v_opens_scrollable_diff_viewer_and_restores_transcript_viewport() -> None:
    async def scenario() -> tuple[tuple[bool, str, float, float, bool, bool], bool, bool]:
        app = TextualTui()
        presentation = _presentation()
        async with app.run_test(size=(60, 12)) as pilot:
            transcript = app.query_one("#transcript", Transcript)
            for index in range(30):
                app.write_assistant(f"message {index}")
            card = app.mount_tool_call("call-1", "edit", {"path": "src/example.py"})
            assert card is not None
            card.set_state("done", detail=presentation)
            card.focus()
            await pilot.pause()
            transcript.stop_following()
            transcript.scroll_to(y=5, animate=False)
            await pilot.pause()
            before = transcript.scroll_y

            await pilot.press("v")
            await pilot.pause()
            viewer = app.query_one("#diff-viewer", DiffViewer)
            body = viewer.query_one("#diff-viewer-body", Static).render().plain
            await pilot._post_mouse_events(
                [events.MouseScrollDown],
                widget=viewer._scroll,
                times=3,
            )
            await pilot.pause()
            wheel_scrolled = viewer._scroll.scroll_y > 0
            await pilot.press("end")
            await pilot.pause()
            viewer_scrolled = viewer._scroll.scroll_y > 0

            await pilot.press("q")
            await pilot.pause()
            await pilot.pause()
            return (
                (
                    viewer.is_open,
                    body,
                    before,
                    transcript.scroll_y,
                    transcript.is_following,
                    app.focused is app.query_one(PromptEditor),
                ),
                wheel_scrolled,
                viewer_scrolled,
            )

    (
        (
            open_after_close,
            body,
            before,
            after,
            following,
            input_focused,
        ),
        wheel_scrolled,
        viewer_scrolled,
    ) = anyio.run(scenario)

    assert not open_after_close
    assert "line 1" in body
    assert "line 79" in body
    assert wheel_scrolled
    assert viewer_scrolled
    assert after == before
    assert not following
    assert input_focused
