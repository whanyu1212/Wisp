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

type DiffViewerSnapshot = tuple[str, str]


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


def _replacement_presentation() -> DiffPresentation:
    return DiffPresentation(
        path="src/replacement.py",
        operation=DiffOperation.modify,
        additions=2,
        deletions=2,
        rows=(
            DiffRow(DiffRowKind.hunk, "@@ -1,3 +1,3 @@"),
            DiffRow(DiffRowKind.context, "before", old_line=1, new_line=1),
            DiffRow(DiffRowKind.deletion, "old one", old_line=2),
            DiffRow(DiffRowKind.deletion, "old two", old_line=3),
            DiffRow(DiffRowKind.addition, "new one", new_line=2),
            DiffRow(DiffRowKind.addition, "new two", new_line=3),
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


def test_viewer_layout_controls_report_effective_layout_and_resize_fallback() -> None:
    async def scenario() -> tuple[
        DiffViewerSnapshot,
        DiffViewerSnapshot,
        DiffViewerSnapshot,
        DiffViewerSnapshot,
        DiffViewerSnapshot,
        DiffViewerSnapshot,
    ]:
        app = TextualTui()
        async with app.run_test(size=(100, 20)) as pilot:
            card = app.mount_tool_call("call-layout", "edit", {"path": "src/replacement.py"})
            assert card is not None
            card.set_state("done", detail=_replacement_presentation())
            card.focus()
            await pilot.pause()
            await pilot.press("v")
            await pilot.pause()
            viewer = app.query_one("#diff-viewer", DiffViewer)
            header = viewer.query_one("#diff-viewer-header", Static)
            body = viewer.query_one("#diff-viewer-body", Static)

            def snapshot() -> DiffViewerSnapshot:
                state = (
                    f"{viewer.requested_layout.value}:"
                    f"{viewer.effective_layout.value}:"
                    f"{header.render().plain}"
                )
                return state, body.render().plain

            auto = snapshot()
            await pilot.press("u")
            await pilot.pause()
            unified = snapshot()
            await pilot.press("s")
            await pilot.pause()
            explicit = snapshot()

            await pilot.resize_terminal(60, 20)
            await pilot.pause()
            narrow = snapshot()
            await pilot.resize_terminal(100, 20)
            await pilot.pause()
            restored = snapshot()
            await pilot.press("a")
            await pilot.pause()
            auto_again = snapshot()
            return auto, unified, explicit, narrow, restored, auto_again

    auto, unified, explicit, narrow, restored, auto_again = anyio.run(scenario)

    def has_paired_replacement(snapshot: DiffViewerSnapshot) -> bool:
        return any("old one" in line and "new one" in line for line in snapshot[1].splitlines())

    assert auto[0].startswith("auto:split:")
    assert "auto→split" in auto[0]
    assert has_paired_replacement(auto)
    assert " │ " in auto[1]
    assert unified[0].startswith("unified:unified:")
    assert not has_paired_replacement(unified)
    assert explicit[0].startswith("split:split:")
    assert has_paired_replacement(explicit)
    assert narrow[0].startswith("split:unified:")
    assert "split→unified" in narrow[0]
    assert not has_paired_replacement(narrow)
    assert restored[0].startswith("split:split:")
    assert has_paired_replacement(restored)
    assert auto_again[0].startswith("auto:split:")
    assert has_paired_replacement(auto_again)
