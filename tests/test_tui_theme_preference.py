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


def test_unreadable_file_is_left_alone_rather_than_overwritten(tmp_path: Path) -> None:
    # A read failure leaves the real contents unknown. Writing anyway would
    # replace unrelated preferences with just the theme — silently losing data
    # while nominally "succeeding". Refusing to save is the safe outcome.
    path = theme_preference_path(home_dir=tmp_path)
    path.parent.mkdir(parents=True)
    original = json.dumps({"unrelated": "kept", "theme": "wisp"})
    path.write_text(original, encoding="utf-8")

    def _explode(*args: object, **kwargs: object) -> str:
        raise PermissionError("read denied")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "read_text", _explode)
        assert not save_theme_preference("wisp-light", home_dir=tmp_path)

    assert path.read_text(encoding="utf-8") == original


def test_failed_write_leaves_the_previous_document_intact(tmp_path: Path) -> None:
    # A direct truncating write would empty the file before failing, losing the
    # previous theme and every unrelated key. Staging through a temp file and
    # renaming means the destination is never observed half-written.
    path = theme_preference_path(home_dir=tmp_path)
    path.parent.mkdir(parents=True)
    original = json.dumps({"unrelated": "kept", "theme": "wisp"}) + "\n"
    path.write_text(original, encoding="utf-8")

    def _explode(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("wisp.tui.theme_preference.os.replace", _explode)
        assert not save_theme_preference("wisp-light", home_dir=tmp_path)

    assert path.read_text(encoding="utf-8") == original
    assert load_theme_preference(home_dir=tmp_path) == "wisp"


def test_failed_write_does_not_leave_temp_files_behind(tmp_path: Path) -> None:
    path = theme_preference_path(home_dir=tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"theme": "wisp"}), encoding="utf-8")

    def _explode(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("wisp.tui.theme_preference.os.replace", _explode)
        assert not save_theme_preference("wisp-light", home_dir=tmp_path)

    assert [entry.name for entry in path.parent.iterdir()] == [path.name]


def test_unparseable_file_is_replaced_rather_than_preserved(tmp_path: Path) -> None:
    # Distinct from an unreadable file: content that parsed to nothing carries
    # no preferences worth keeping, so overwriting it repairs rather than loses.
    path = theme_preference_path(home_dir=tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")

    assert save_theme_preference("wisp-light", home_dir=tmp_path)

    assert load_theme_preference(home_dir=tmp_path) == "wisp-light"


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


def test_unreadable_file_loads_as_unset_rather_than_raising(tmp_path: Path) -> None:
    # Load is forgiving where save is conservative: startup must never fail
    # because the preference file could not be read.
    path = theme_preference_path(home_dir=tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"theme": "wisp-light"}), encoding="utf-8")

    def _explode(*args: object, **kwargs: object) -> str:
        raise PermissionError("read denied")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "read_text", _explode)
        assert load_theme_preference(home_dir=tmp_path) is None


def test_invalid_utf8_loads_as_unset_rather_than_crashing_startup(tmp_path: Path) -> None:
    # Reading fails at two layers and only one is an OSError: UnicodeDecodeError
    # subclasses ValueError. A partial or corrupted write produces exactly this,
    # and because loading runs during on_mount an escaping exception would stop
    # the TUI from starting rather than merely losing the theme.
    path = theme_preference_path(home_dir=tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"theme": "\xff\xfe not utf-8"}')

    assert load_theme_preference(home_dir=tmp_path) is None


def test_invalid_utf8_is_not_overwritten_by_a_save(tmp_path: Path) -> None:
    # Undecodable bytes are unreadable, not empty: the real contents are unknown,
    # so saving must refuse rather than replace whatever was there.
    path = theme_preference_path(home_dir=tmp_path)
    path.parent.mkdir(parents=True)
    original = b'{"theme": "\xff\xfe not utf-8"}'
    path.write_bytes(original)

    assert not save_theme_preference("wisp-light", home_dir=tmp_path)

    assert path.read_bytes() == original


def test_startup_survives_an_undecodable_preference_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The end-to-end form of the same guarantee, through a real app mount.
    from wisp.tui.textual_app import TextualTui

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    path = theme_preference_path(home_dir=tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe\x00garbage")

    async def scenario() -> str:
        app = TextualTui()
        async with app.run_test() as pilot:
            await pilot.pause()
            return str(app.theme)

    assert anyio.run(scenario) == WISP_THEMES[0].name


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
