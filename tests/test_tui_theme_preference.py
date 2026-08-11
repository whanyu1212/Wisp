"""Tests for the persisted TUI theme choice."""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

from wisp.tui.theme import WISP_THEME_NAMES, WISP_THEMES
from wisp.tui.theme_preference import (
    load_theme_preference,
    save_theme_preference,
    theme_preference_path,
)

pytestmark = pytest.mark.tui


def test_preference_lives_beside_other_user_local_client_state(tmp_path: Path) -> None:
    # Deliberately NOT in the agent settings file: theme is presentation state
    # the RPC subprocess never reads, and settings can be project-influenced.
    path = theme_preference_path(home_dir=tmp_path)

    assert path == tmp_path / ".wisp" / "tui.json"


def test_missing_preference_is_unset_rather_than_an_error(tmp_path: Path) -> None:
    assert load_theme_preference(home_dir=tmp_path) is None


def test_saved_theme_round_trips(tmp_path: Path) -> None:
    assert save_theme_preference("wisp-light", home_dir=tmp_path)

    assert load_theme_preference(home_dir=tmp_path) == "wisp-light"


def test_save_creates_the_parent_directory(tmp_path: Path) -> None:
    # First run on a fresh machine has no ~/.wisp yet.
    assert not (tmp_path / ".wisp").exists()

    assert save_theme_preference("wisp", home_dir=tmp_path)
    assert load_theme_preference(home_dir=tmp_path) == "wisp"


def test_save_preserves_unrelated_keys(tmp_path: Path) -> None:
    # The file is the home for client-side preferences generally, so writing a
    # theme must not discard a neighbouring setting written by something else.
    path = theme_preference_path(home_dir=tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"unrelated": "kept"}), encoding="utf-8")

    assert save_theme_preference("wisp-light", home_dir=tmp_path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {"unrelated": "kept", "theme": "wisp-light"}


@pytest.mark.parametrize(
    "contents",
    ["not json at all", '"a bare string"', "[]", "{}", '{"theme": ""}', '{"theme": 42}'],
)
def test_malformed_preference_falls_back_instead_of_raising(tmp_path: Path, contents: str) -> None:
    # Matches how settings files are treated: unusable input is skipped, never
    # fatal. A corrupt preference must not stop the TUI from starting.
    path = theme_preference_path(home_dir=tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(contents, encoding="utf-8")

    assert load_theme_preference(home_dir=tmp_path) is None


def test_unknown_theme_is_rejected_when_valid_names_are_supplied(tmp_path: Path) -> None:
    # Textual registers ~20 built-in themes alongside Wisp's own. Adopting one
    # would leave transcript role colors and diff variables unresolvable.
    assert save_theme_preference("dracula", home_dir=tmp_path)

    assert load_theme_preference(home_dir=tmp_path) == "dracula"
    assert load_theme_preference(home_dir=tmp_path, valid_themes=WISP_THEME_NAMES) is None


def test_wisp_theme_names_covers_every_defined_theme() -> None:
    assert WISP_THEME_NAMES == {theme.name for theme in WISP_THEMES}


def test_toggle_cycles_themes_and_persists_the_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wisp.tui.textual_app import TextualTui

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    async def scenario() -> tuple[str, str, str, str | None]:
        app = TextualTui()
        async with app.run_test() as pilot:
            start = app.theme
            await pilot.press("ctrl+t")
            await pilot.pause()
            switched = app.theme
            await pilot.press("ctrl+t")
            await pilot.pause()
            return start, switched, app.theme, load_theme_preference(home_dir=tmp_path)

    start, switched, returned, persisted = anyio.run(scenario)

    assert start == WISP_THEMES[0].name
    assert switched == WISP_THEMES[1].name
    assert returned == start
    # The last toggle is what persists.
    assert persisted == start


def test_startup_adopts_a_persisted_theme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from wisp.tui.textual_app import TextualTui

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert save_theme_preference(WISP_THEMES[1].name, home_dir=tmp_path)

    async def scenario() -> str:
        app = TextualTui()
        async with app.run_test() as pilot:
            await pilot.pause()
            return str(app.theme)

    assert anyio.run(scenario) == WISP_THEMES[1].name


def test_startup_ignores_a_persisted_non_wisp_theme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wisp.tui.textual_app import TextualTui

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert save_theme_preference("dracula", home_dir=tmp_path)

    async def scenario() -> str:
        app = TextualTui()
        async with app.run_test() as pilot:
            await pilot.pause()
            return str(app.theme)

    assert anyio.run(scenario) == WISP_THEMES[0].name
