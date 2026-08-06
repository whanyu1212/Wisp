"""Tests for the inline ``@``-file picker widget and its app wiring."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from wisp.events import TrustRequested
from wisp.tui.file_suggest import FileSuggest
from wisp.tui.textual_app import TextualTui
from wisp.tui.widgets import PromptEditor, SlashSuggest

pytestmark = pytest.mark.tui

_CORPUS = ("README.md", "src/", "src/app.py", "src/wisp/tui/textual_app.py")


# --- trigger grammar (pure) ------------------------------------------------


@pytest.mark.parametrize(
    ("value", "cursor", "expected"),
    [
        ("@", 1, ""),
        ("@app", 4, "app"),
        ("look at @src/app", 16, "src/app"),
        ("@app extra", 4, "app"),
        # No mention before the cursor.
        ("plain text", 10, None),
        # Mid-word `@` is an email or decorator, not a mention.
        ("mail@example.com", 16, None),
        # A space closes the mention.
        ("@app done", 9, None),
        # The cursor sits before the `@`.
        ("@app", 0, None),
    ],
)
def test_query_from_value(value: str, cursor: int, expected: str | None) -> None:
    assert FileSuggest.query_from_value(value, cursor) == expected


def test_query_from_value_rejects_out_of_range_cursor() -> None:
    assert FileSuggest.query_from_value("@app", 99) is None
    assert FileSuggest.query_from_value("@app", -1) is None


# --- widget behavior -------------------------------------------------------


def test_show_for_without_corpus_stays_hidden() -> None:
    """Before the background walk lands there is nothing to offer."""

    async def scenario() -> bool:
        app = TextualTui()
        async with app.run_test(size=(80, 24)):
            picker = app.query_one("#file-suggest", FileSuggest)
            assert picker.show_for("@", 1) == 0
            return picker.is_open

    assert anyio.run(scenario) is False


def test_typing_at_opens_picker_and_filters() -> None:
    async def scenario() -> tuple[bool, int, str | None]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            picker.set_paths(_CORPUS)

            await pilot.press("@")
            await pilot.pause()
            opened = picker.is_open

            for key in "app":
                await pilot.press(key)
            await pilot.pause()
            return opened, picker.option_count, picker.highlighted_path()

    opened, count, highlighted = anyio.run(scenario)
    assert opened is True
    assert count > 0
    assert highlighted == "src/app.py"


def test_enter_inserts_path_and_closes() -> None:
    async def scenario() -> tuple[str, bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            picker.set_paths(_CORPUS)
            editor = app.query_one("#input", PromptEditor)

            for key in "@app":
                await pilot.press(key)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            return editor.value, picker.is_open

    value, still_open = anyio.run(scenario)
    assert value == "@src/app.py "
    assert still_open is False


def test_completion_preserves_surrounding_prose() -> None:
    """The mention is spliced; the rest of the line must survive intact."""

    async def scenario() -> str:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            picker.set_paths(_CORPUS)
            editor = app.query_one("#input", PromptEditor)

            editor.value = "please read "
            editor.cursor_position = len("please read ")
            for key in "@app":
                await pilot.press(key)
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            return editor.value

    assert anyio.run(scenario) == "please read @src/app.py "


def test_path_with_space_is_quoted() -> None:
    async def scenario() -> str:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            picker.set_paths(("my notes.md",))
            editor = app.query_one("#input", PromptEditor)

            for key in "@notes":
                await pilot.press(key)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            return editor.value

    assert anyio.run(scenario) == '@"my notes.md" '


def test_escape_dismisses_but_keeps_typed_text() -> None:
    async def scenario() -> tuple[bool, str]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            picker.set_paths(_CORPUS)
            editor = app.query_one("#input", PromptEditor)

            for key in "@app":
                await pilot.press(key)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            return picker.is_open, editor.value

    still_open, value = anyio.run(scenario)
    assert still_open is False
    assert value == "@app"


def test_arrow_keys_move_highlight() -> None:
    async def scenario() -> tuple[str | None, str | None]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            picker.set_paths(_CORPUS)

            await pilot.press("@")
            await pilot.pause()
            first = picker.highlighted_path()
            await pilot.press("down")
            await pilot.pause()
            return first, picker.highlighted_path()

    first, second = anyio.run(scenario)
    assert first is not None
    assert second is not None
    assert first != second


def test_picker_never_takes_focus() -> None:
    """The caret must stay in the editor or typed keys land in the OptionList."""

    async def scenario() -> bool:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            picker.set_paths(_CORPUS)
            editor = app.query_one("#input", PromptEditor)

            for key in "@app":
                await pilot.press(key)
            await pilot.pause()
            return editor.has_focus

    assert anyio.run(scenario) is True


def test_mid_word_at_does_not_open_picker() -> None:
    async def scenario() -> bool:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            picker.set_paths(_CORPUS)

            for key in "mail@x":
                await pilot.press(key)
            await pilot.pause()
            return picker.is_open

    assert anyio.run(scenario) is False


def test_slash_menu_still_works_and_menus_are_exclusive() -> None:
    """The `@` picker must not regress the existing slash menu."""

    async def scenario() -> tuple[bool, bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            picker.set_paths(_CORPUS)
            suggest = app.query_one("#suggest", SlashSuggest)

            await pilot.press("/")
            await pilot.pause()
            return suggest.is_open, picker.is_open

    slash_open, file_open = anyio.run(scenario)
    assert slash_open is True
    assert file_open is False


def test_late_corpus_opens_a_mention_typed_while_indexing() -> None:
    """Typing `@query` before the walk lands must not require an extra keystroke.

    `show_for` hides while the corpus is empty, so installing it later has to
    re-evaluate the editor or the picker stays hidden until the user types again.
    """

    async def scenario() -> bool:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            # Corpus deliberately not installed yet: the walk is still running.
            for key in "@app":
                await pilot.press(key)
            await pilot.pause()
            assert picker.is_open is False

            app._install_file_suggestions(picker, _CORPUS)  # noqa: SLF001 - worker callback
            await pilot.pause()
            return picker.is_open

    assert anyio.run(scenario) is True


def test_late_corpus_does_not_open_the_picker_without_a_mention() -> None:
    """Installing the corpus is not itself a trigger — the caret must be in a mention."""

    async def scenario() -> bool:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            for key in "hello":
                await pilot.press(key)
            await pilot.pause()

            app._install_file_suggestions(picker, _CORPUS)  # noqa: SLF001 - worker callback
            await pilot.pause()
            return picker.is_open

    assert anyio.run(scenario) is False


def test_late_corpus_yields_to_a_live_slash_menu() -> None:
    """Menu exclusivity survives the late-install path."""

    async def scenario() -> tuple[bool, bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            suggest = app.query_one("#suggest", SlashSuggest)
            await pilot.press("/")
            await pilot.pause()

            app._install_file_suggestions(picker, _CORPUS)  # noqa: SLF001 - worker callback
            await pilot.pause()
            return suggest.is_open, picker.is_open

    slash_open, file_open = anyio.run(scenario)
    assert slash_open is True
    assert file_open is False


def test_opening_an_overlay_hides_the_open_picker() -> None:
    """An overlay hides the composer, so its `@` picker must not float over it."""

    async def scenario() -> bool:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            picker.set_paths(_CORPUS)
            for key in "@app":
                await pilot.press(key)
            await pilot.pause()
            assert picker.is_open is True

            app.show_trust(TrustRequested(request_id="trust-1", project_path=Path("/work/project")))
            await pilot.pause()
            return picker.is_open

    assert anyio.run(scenario) is False


def test_late_corpus_does_not_reopen_the_picker_under_an_overlay() -> None:
    """A background walk landing mid-overlay must not revive the picker.

    The worker's arrival is not user intent; reviving here would put the picker
    back over an active approval and steal its Escape.
    """

    async def scenario() -> bool:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            for key in "@app":
                await pilot.press(key)
            await pilot.pause()

            app.show_trust(TrustRequested(request_id="trust-1", project_path=Path("/work/project")))
            await pilot.pause()

            app._install_file_suggestions(picker, _CORPUS)  # noqa: SLF001 - worker callback
            await pilot.pause()
            return picker.is_open

    assert anyio.run(scenario) is False


def test_protected_paths_never_reach_the_picker(tmp_path: Path) -> None:
    """End-to-end: a real walk must not surface `.env` as mentionable."""

    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "app.py").write_text("x", encoding="utf-8")

    async def scenario() -> tuple[str, ...]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            app.load_file_suggestions(str(tmp_path))
            # Let the threaded walk finish and post its result back.
            for _ in range(50):
                await pilot.pause()
                picker = app.query_one("#file-suggest", FileSuggest)
                if picker.has_paths:
                    break
            picker = app.query_one("#file-suggest", FileSuggest)
            picker.show_for("@", 1)
            return tuple(
                option.id or ""
                for option in picker._options  # noqa: SLF001 - asserting rendered rows
            )

    options = anyio.run(scenario)
    assert "app.py" in options
    assert ".env" not in options
