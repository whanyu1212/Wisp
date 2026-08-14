from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from wisp.tui.overlay import OverlayKind
from wisp.tui.theme import PAPER_THEME_NAME
from wisp.tui.theme_picker import ThemePicker
from wisp.tui.theme_preference import load_theme_state
from wisp.tui.widgets import PromptEditor, Transcript

pytestmark = pytest.mark.tui


def test_bare_theme_command_previews_then_escape_restores_without_queueing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wisp.tui.textual_app import TextualTui

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    async def scenario() -> tuple[str, str, bool, int, str]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one(PromptEditor)
            editor.value = "draft"
            app.submit_command_line("/theme")
            await pilot.pause()
            picker = app.query_one(ThemePicker)
            opened = picker.is_open
            await pilot.press("down")
            await pilot.pause()
            preview = app.theme
            await pilot.press("escape")
            await pilot.pause()
            queued = app._input_controller.receive_stream.statistics().current_buffer_used
            return preview, app.theme, opened, queued, editor.value

    preview, restored, opened, queued, draft = anyio.run(scenario)
    assert opened
    assert preview == "wisp-orchid"
    assert restored == "wisp"
    assert queued == 0
    assert draft == ""
    assert load_theme_state(home_dir=tmp_path).active_theme is None


def test_theme_selection_persists_and_ctrl_t_returns_to_most_recent_dark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wisp.tui.textual_app import TextualTui

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    async def scenario() -> tuple[str, str, str]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            app.submit_command_line("/theme ember")
            await pilot.pause()
            direct = app.theme
            await pilot.press("ctrl+t")
            await pilot.pause()
            light = app.theme
            await pilot.press("ctrl+t")
            await pilot.pause()
            return direct, light, app.theme

    direct, light, returned = anyio.run(scenario)
    state = load_theme_state(home_dir=tmp_path)
    assert (direct, light, returned) == ("wisp-ember", PAPER_THEME_NAME, "wisp-ember")
    assert state.active_theme == "wisp-ember"
    assert state.last_dark_theme == "wisp-ember"


def test_ctrl_t_does_not_commit_while_picker_owns_a_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wisp.tui.textual_app import TextualTui

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    async def scenario() -> tuple[str, str]:
        app = TextualTui()
        async with app.run_test() as pilot:
            app.submit_command_line("/theme")
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            preview = app.theme
            await pilot.press("ctrl+t", "escape")
            await pilot.pause()
            return preview, app.theme

    preview, restored = anyio.run(scenario)
    assert preview == "wisp-orchid"
    assert restored == "wisp"
    assert load_theme_state(home_dir=tmp_path).active_theme is None


def test_replacing_picker_with_another_overlay_rolls_back_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wisp.tui.textual_app import TextualTui

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    async def scenario() -> tuple[str, str, str | None]:
        app = TextualTui()
        async with app.run_test() as pilot:
            app.submit_command_line("/theme")
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            preview = app.theme
            assert app._overlay_controller is not None
            app._overlay_controller.open(OverlayKind.decision)
            await pilot.pause()
            return preview, app.theme, app._theme_picker_original

    preview, restored, original = anyio.run(scenario)
    assert preview == "wisp-orchid"
    assert restored == "wisp"
    assert original is None
    assert load_theme_state(home_dir=tmp_path).active_theme is None


def test_queued_selection_is_ignored_after_picker_displacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wisp.tui.textual_app import TextualTui

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    async def scenario() -> tuple[str, str | None]:
        app = TextualTui()
        async with app.run_test() as pilot:
            app.submit_command_line("/theme")
            await pilot.pause()
            picker = app.query_one(ThemePicker)
            picker.post_message(ThemePicker.Selected("wisp-orchid"))
            assert app._overlay_controller is not None
            app._overlay_controller.open(OverlayKind.decision)
            await pilot.pause()
            return app.theme, app._theme_picker_original

    restored, original = anyio.run(scenario)
    assert restored == "wisp"
    assert original is None
    assert load_theme_state(home_dir=tmp_path).active_theme is None


def test_multiline_theme_prefix_remains_prompt_content() -> None:
    from wisp.tui.textual_app import TextualTui

    prompt = "/theme\nember"

    async def scenario() -> str:
        app = TextualTui()
        async with app.run_test() as pilot:
            app.submit_command_line(prompt)
            await pilot.pause()
            queued = await app._input_controller.receive_stream.receive()
            assert isinstance(queued, str)
            return queued

    assert anyio.run(scenario) == prompt


def test_theme_picker_commit_preserves_viewport_and_uses_latest_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wisp.tui.textual_app import TextualTui

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    async def scenario() -> tuple[str, float, float]:
        app = TextualTui()
        async with app.run_test(size=(80, 16)) as pilot:
            for index in range(60):
                app.write_user(f"message {index}")
            await pilot.pause()
            transcript = app.query_one(Transcript)
            transcript.stop_following()
            transcript.scroll_to(y=8, animate=False)
            await pilot.pause()
            before = transcript.scroll_y
            app.submit_command_line("/theme")
            await pilot.pause()
            await pilot.press("down", "down")
            await pilot.pause()
            preview = app.theme
            await pilot.press("enter")
            await pilot.pause()
            return preview, before, transcript.scroll_y

    preview, before, after = anyio.run(scenario)
    assert preview == "wisp-ember"
    assert after == before


@pytest.mark.parametrize(
    "command",
    ["/theme unknown", "/theme vapor extra", "/THEME unknown"],
)
def test_invalid_theme_command_is_local_and_never_queued(command: str) -> None:
    from wisp.tui.textual_app import TextualTui

    async def scenario() -> int:
        app = TextualTui()
        async with app.run_test() as pilot:
            app.submit_command_line(command)
            await pilot.pause()
            return app._input_controller.receive_stream.statistics().current_buffer_used

    assert anyio.run(scenario) == 0
