"""Tests for the inline ``@``-file picker widget and its app wiring."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from wisp.events import TrustRequested
from wisp.tui.file_index import (
    FileIndexRequest,
    ProjectChildren,
    ProjectDirectory,
    ProjectFile,
    ProjectSnapshot,
    SnapshotTruncation,
)
from wisp.tui.file_suggest import FilePickerMode, FileSuggest
from wisp.tui.textual_app import TextualTui
from wisp.tui.widgets import PromptEditor, SlashSuggest

pytestmark = pytest.mark.tui

_CORPUS = ("README.md", "src/", "src/app.py", "src/wisp/tui/textual_app.py")


def _snapshot(paths: tuple[str, ...], root: Path = Path("/work")) -> ProjectSnapshot:
    entries = tuple(
        ProjectDirectory(path.removesuffix("/")) if path.endswith("/") else ProjectFile(path)
        for path in paths
    )
    return ProjectSnapshot(root=root.resolve(strict=False), entries=entries)


def _tree_snapshot(*, truncated: bool = False, root: Path = Path("/work")) -> ProjectSnapshot:
    return ProjectSnapshot(
        root=root.resolve(strict=False),
        entries=(
            ProjectFile("README.md"),
            ProjectDirectory("src"),
            ProjectFile("src/app.py"),
            ProjectDirectory("src/wisp"),
            ProjectDirectory("src/wisp/tui"),
            ProjectFile("src/wisp/tui/textual_app.py"),
            ProjectFile("文 件.md"),
        ),
        child_adjacency=(
            ProjectChildren("", ("README.md", "src", "文 件.md")),
            ProjectChildren("src", ("src/app.py", "src/wisp")),
            ProjectChildren("src/wisp", ("src/wisp/tui",)),
            ProjectChildren("src/wisp/tui", ("src/wisp/tui/textual_app.py",)),
        ),
        truncation=SnapshotTruncation(
            entry_limit_reached=truncated,
            depth_limit_reached=truncated,
        ),
    )


def _set_paths(picker: FileSuggest, paths: tuple[str, ...]) -> None:
    picker.set_snapshot(_snapshot(paths))


def _deliver(app: TextualTui, picker: FileSuggest, paths: tuple[str, ...]) -> None:
    """Deliver one deterministic worker result through the generation gate."""

    app._file_index_generation += 1  # noqa: SLF001 - lifecycle test seam
    request = FileIndexRequest(
        generation=app._file_index_generation,  # noqa: SLF001
        cwd="/work",
    )
    app._file_index_request = request  # noqa: SLF001
    app._install_file_suggestions(  # noqa: SLF001 - simulated worker callback
        request, picker, _snapshot(paths)
    )


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


def test_show_for_without_corpus_is_logically_active_for_tree_toggle() -> None:
    """Loading/no-results state still owns contextual Tab so tree is reachable."""

    async def scenario() -> tuple[bool, bool, FilePickerMode]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            assert picker.show_for("@", 1) == 0
            await pilot.press("tab")
            return picker.is_open, picker.is_active, picker.mode

    is_open, active, mode = anyio.run(scenario)
    assert is_open is True
    assert active is True
    assert mode is FilePickerMode.TREE


def test_typing_at_opens_picker_and_filters() -> None:
    async def scenario() -> tuple[bool, int, str | None]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            _set_paths(picker, _CORPUS)

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
            _set_paths(picker, _CORPUS)
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


def test_directory_is_visible_and_completion_preserves_trailing_slash() -> None:
    async def scenario() -> tuple[tuple[str, ...], str]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            _set_paths(picker, _CORPUS)
            editor = app.query_one("#input", PromptEditor)

            for key in "@src":
                await pilot.press(key)
            await pilot.pause()
            visible = picker.visible_paths
            # Keep navigation bounded: if directory ranking regresses, this test must
            # fail rather than leave Textual's pilot loop spinning indefinitely.
            for _ in visible:
                if picker.highlighted_path() == "src/":
                    break
                await pilot.press("down")
            assert picker.highlighted_path() == "src/"
            await pilot.press("enter")
            await pilot.pause()
            return visible, editor.value

    visible, value = anyio.run(scenario)
    assert "src/" in visible
    assert value == "@src/ "


def test_completion_preserves_surrounding_prose() -> None:
    """The mention is spliced; the rest of the line must survive intact."""

    async def scenario() -> str:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            _set_paths(picker, _CORPUS)
            editor = app.query_one("#input", PromptEditor)

            editor.value = "please read "
            editor.cursor_position = len("please read ")
            for key in "@app":
                await pilot.press(key)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            return editor.value

    assert anyio.run(scenario) == "please read @src/app.py "


def test_path_with_space_is_quoted() -> None:
    async def scenario() -> str:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            _set_paths(picker, ("my notes.md",))
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
            _set_paths(picker, _CORPUS)
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
            _set_paths(picker, _CORPUS)

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


def test_keyboard_caret_movement_updates_then_closes_the_mention() -> None:
    async def scenario() -> tuple[str, bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            _set_paths(picker, _CORPUS)
            await pilot.press("@", "a", "p", "p", "left")
            await pilot.pause()
            within_query = picker.current_query
            await pilot.press("left", "left", "left")
            await pilot.pause()
            return within_query, picker.is_active

    assert anyio.run(scenario) == ("ap", False)


def test_mouse_caret_movement_updates_then_closes_the_mention() -> None:
    async def scenario() -> tuple[str, bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            _set_paths(picker, _CORPUS)
            editor = app.query_one("#input", PromptEditor)
            editor.value = "@app tail"
            editor.cursor_position = len(editor.value)
            await pilot.pause()

            assert await pilot.click("#input", offset=(3, 0))
            await pilot.pause()
            within_query = picker.current_query
            assert await pilot.click("#input", offset=(5, 0))
            await pilot.pause()
            return within_query, picker.is_active

    assert anyio.run(scenario) == ("ap", False)


def test_picker_never_takes_focus() -> None:
    """The caret must stay in the editor or typed keys land in the OptionList."""

    async def scenario() -> bool:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            _set_paths(picker, _CORPUS)
            editor = app.query_one("#input", PromptEditor)

            for key in "@app":
                await pilot.press(key)
            await pilot.pause()
            return editor.has_focus

    assert anyio.run(scenario) is True


def test_tab_toggles_modes_without_completing_and_preserves_query_selection() -> None:
    async def scenario() -> tuple[object, ...]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            picker.set_snapshot(_tree_snapshot())
            editor = app.query_one("#input", PromptEditor)

            await pilot.press("@", "a", "p", "p")
            await pilot.pause()
            initial = (picker.mode, picker.current_query, picker.selected_path, editor.value)
            await pilot.press("tab")
            await pilot.pause()
            tree = (picker.mode, picker.current_query, picker.selected_path, editor.value)
            await pilot.press("tab")
            await pilot.pause()
            return (*initial, *tree, picker.mode, picker.selected_path, editor.value)

    result = anyio.run(scenario)
    assert result == (
        FilePickerMode.FUZZY,
        "app",
        "src/app.py",
        "@app",
        FilePickerMode.TREE,
        "app",
        "src/app.py",
        "@app",
        FilePickerMode.FUZZY,
        "src/app.py",
        "@app",
    )


def test_no_match_can_toggle_to_tree_and_reveals_fuzzy_selected_nested_path() -> None:
    async def scenario() -> tuple[tuple[str, ...], tuple[str, ...], str]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            picker.set_snapshot(_tree_snapshot())
            editor = app.query_one("#input", PromptEditor)

            await pilot.press("@", "t", "u", "i", "a", "p", "p")
            await pilot.pause()
            selected = picker.selected_path
            await pilot.press("tab")
            await pilot.pause()
            revealed = picker.visible_paths

            await pilot.press("tab")  # return to fuzzy
            editor.value = "@does-not-match"
            editor.cursor_position = len(editor.value)
            await pilot.pause()
            assert picker.visible_paths == () and picker.is_active
            await pilot.press("tab")
            await pilot.pause()
            return revealed, picker.visible_paths, selected or ""

    revealed, after_no_match_toggle, selected = anyio.run(scenario)
    assert selected == "src/wisp/tui/textual_app.py"
    assert "src/wisp/tui/textual_app.py" in revealed
    assert "README.md" in after_no_match_toggle


def test_tree_directory_enter_and_left_right_only_expand_or_collapse() -> None:
    async def scenario() -> tuple[object, ...]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            picker.set_snapshot(_tree_snapshot())
            editor = app.query_one("#input", PromptEditor)
            await pilot.press("@", "s", "r", "c", "tab")
            await pilot.pause()
            assert picker.selected_path == "src/"

            await pilot.press("enter")
            await pilot.pause()
            after_enter = (editor.value, picker.visible_paths)
            await pilot.press("left")
            await pilot.pause()
            after_left = picker.visible_paths
            await pilot.press("right")
            await pilot.pause()
            return (*after_enter, after_left, picker.visible_paths, editor.value)

    draft, expanded, collapsed, expanded_again, final_draft = anyio.run(scenario)
    assert draft == "@src"
    assert "src/app.py" in expanded
    assert "src/app.py" not in collapsed
    assert "src/app.py" in expanded_again
    assert final_draft == "@src"


def test_tree_directory_click_expands_without_stealing_editor_focus() -> None:
    async def scenario() -> tuple[str, tuple[str, ...], bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            picker.set_snapshot(_tree_snapshot())
            editor = app.query_one("#input", PromptEditor)
            await pilot.press("@", "s", "r", "c", "tab")
            await pilot.pause()
            assert picker.selected_path == "src/"

            # The selected root directory is the second rendered tree row.
            assert await pilot.click("#file-picker-tree", offset=(2, 2))
            await pilot.pause()
            return editor.value, picker.visible_paths, editor.has_focus

    draft, visible, focused = anyio.run(scenario)
    assert draft == "@src"
    assert "src/app.py" in visible
    assert focused is True


def test_tree_file_enter_uses_reference_formatter_for_unicode_quoted_path() -> None:
    async def scenario() -> str:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            picker.set_snapshot(_tree_snapshot())
            editor = app.query_one("#input", PromptEditor)
            await pilot.press("@", "文", "tab")
            await pilot.pause()
            assert picker.selected_path == "文 件.md"
            await pilot.press("enter")
            await pilot.pause()
            return editor.value

    assert anyio.run(scenario) == '@"文 件.md" '


def test_mouse_activation_uses_shared_seam_and_restores_editor_focus() -> None:
    async def scenario() -> tuple[str, bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            _set_paths(picker, ("my notes.md",))
            editor = app.query_one("#input", PromptEditor)
            await pilot.press("@", "n", "o", "t", "e", "s")
            await pilot.pause()
            # Click the rendered fuzzy row while the editor retains caret ownership.
            assert await pilot.click("#file-picker-fuzzy", offset=(2, 1))
            await pilot.pause()
            return editor.value, editor.has_focus

    value, focused = anyio.run(scenario)
    assert value == '@"my notes.md" '
    assert focused is True


def test_snapshot_refresh_preserves_valid_selection_then_falls_back_deterministically() -> None:
    async def scenario() -> tuple[str | None, str | None]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            _set_paths(picker, ("a.py", "b.py"))
            await pilot.press("@")
            await pilot.press("down")
            await pilot.pause()
            assert picker.selected_path == "b.py"

            _set_paths(picker, ("b.py", "c.py"))
            preserved = picker.selected_path
            _set_paths(picker, ("c.py", "d.py"))
            return preserved, picker.selected_path

    assert anyio.run(scenario) == ("b.py", "c.py")


def test_no_match_to_match_snapshot_refresh_selects_first_visible_result() -> None:
    async def scenario() -> tuple[tuple[str, ...], str | None]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            _set_paths(picker, ("README.md", "src/app.py"))
            await pilot.press("@", "n", "o", "m", "a", "t", "c", "h")
            await pilot.pause()
            assert picker.visible_paths == ()

            _set_paths(picker, ("README.md", "nomatch.py"))
            return picker.visible_paths, picker.selected_path

    assert anyio.run(scenario) == (("nomatch.py",), "nomatch.py")


def test_truncation_cues_are_literal_at_narrow_width() -> None:
    async def scenario() -> tuple[str, int]:
        app = TextualTui()
        async with app.run_test(size=(32, 14)):
            picker = app.query_one("#file-suggest", FileSuggest)
            picker.set_snapshot(_tree_snapshot(truncated=True))
            picker.show_for("@", 1)
            return picker._status_text(), picker._max_width  # noqa: SLF001 - presentation contract

    cue, width = anyio.run(scenario)
    assert "entry limit reached" in cue
    assert "depth limit reached" in cue
    assert width <= 28


def test_tree_expansion_uses_snapshot_only(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> tuple[str, ...]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            picker.set_snapshot(_tree_snapshot())
            monkeypatch.setattr(
                "wisp.tui.file_index.os.scandir",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("filesystem scan")),
            )
            await pilot.press("@", "s", "r", "c", "tab", "right")
            await pilot.pause()
            return picker.visible_paths

    assert "src/app.py" in anyio.run(scenario)


def test_slash_tab_shift_tab_and_plain_tab_regressions() -> None:
    async def slash_tab() -> str:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            await pilot.press("/", "m", "o", "d", "tab")
            await pilot.pause()
            return editor.value

    async def shift_tab() -> tuple[str, FilePickerMode]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            _set_paths(picker, _CORPUS)
            await pilot.press("@", "shift+tab")
            await pilot.pause()
            with anyio.fail_after(2):
                prompt = await app.read_prompt("wisp> ")
            return prompt, picker.mode

    async def plain_tab() -> tuple[str, bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.value = "ordinary"
            editor.cursor_position = len(editor.value)
            await pilot.press("tab")
            await pilot.pause()
            picker = app.query_one("#file-suggest", FileSuggest)
            return editor.value, picker.is_active

    assert anyio.run(slash_tab) == "/model "
    assert anyio.run(shift_tab) == ("/plan", FilePickerMode.FUZZY)
    assert anyio.run(plain_tab) == ("ordinary", False)


def test_mid_word_at_does_not_open_picker() -> None:
    async def scenario() -> bool:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            _set_paths(picker, _CORPUS)

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
            _set_paths(picker, _CORPUS)
            suggest = app.query_one("#suggest", SlashSuggest)

            await pilot.press("/")
            await pilot.pause()
            return suggest.is_open, picker.is_open

    slash_open, file_open = anyio.run(scenario)
    assert slash_open is True
    assert file_open is False


def test_empty_snapshot_replacement_closes_old_results() -> None:
    async def scenario() -> tuple[bool, tuple[str, ...]]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            _set_paths(picker, _CORPUS)
            await pilot.press("@")
            await pilot.pause()
            assert picker.is_open is True

            picker.set_snapshot(_snapshot(()))
            return picker.is_active, picker.visible_paths

    active, options = anyio.run(scenario)
    assert active is True
    assert options == ()


def test_late_corpus_opens_a_mention_typed_while_indexing() -> None:
    """Typing `@query` before the walk lands must not require an extra keystroke.

    Installing the corpus later has to re-evaluate the editor so the loading
    presentation is replaced without requiring another keystroke.
    """

    async def scenario() -> bool:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            # Corpus deliberately not installed yet: the walk is still running.
            for key in "@app":
                await pilot.press(key)
            await pilot.pause()
            assert picker.is_active is True

            _deliver(app, picker, _CORPUS)  # noqa: SLF001 - worker callback
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

            _deliver(app, picker, _CORPUS)  # noqa: SLF001 - worker callback
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

            _deliver(app, picker, _CORPUS)  # noqa: SLF001 - worker callback
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
            _set_paths(picker, _CORPUS)
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

            _deliver(app, picker, _CORPUS)  # noqa: SLF001 - worker callback
            await pilot.pause()
            return picker.is_open

    assert anyio.run(scenario) is False


def test_adopted_auth_paths_accumulate_and_keep_the_snapshot_tristate() -> None:
    """Mid-session credential changes must narrow, never widen or clobber.

    They are held beside `_protected_paths` so `None` keeps meaning "nothing was
    supplied" — merging would silently collapse an embedded caller's fallback
    resolution into a single glob.
    """

    async def scenario() -> tuple[object, tuple[str, ...]]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)):
            app.set_picker_auth_path(Path("/work/first-auth.json"))
            app.set_picker_auth_path(Path("/work/second-auth.json"))
            # Idempotent: re-adopting must not duplicate.
            app.set_picker_auth_path(Path("/work/second-auth.json"))
            return app._protected_paths, app._adopted_auth_paths  # noqa: SLF001

    snapshot, adopted = anyio.run(scenario)
    assert snapshot is None
    # The superseded credential stays protected: a config change never widens.
    assert adopted == ("/work/first-auth.json", "/work/second-auth.json")


def test_reverse_order_cwd_completions_keep_newest_snapshot(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()

    async def scenario() -> tuple[Path | None, bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)):
            picker = app.query_one("#file-suggest", FileSuggest)
            pending: list[FileIndexRequest] = []
            app._start_file_index_request = (  # type: ignore[method-assign]  # noqa: SLF001
                lambda request, _picker: pending.append(request)
            )
            first = app.load_file_suggestions(str(root_a))
            assert first is not None
            app._install_file_suggestions(  # noqa: SLF001
                first, picker, _snapshot(("old.py",), root_a)
            )
            picker.show_for("@old", 4)
            assert picker.is_open is True

            second = app.load_file_suggestions(str(root_b))
            assert second is not None
            # CWD transition fails closed before work; a loading shell may remain
            # active, but no old path can be activated.
            assert picker.snapshot is None
            assert picker.selected_path is None
            assert picker.visible_paths == ()

            app._install_file_suggestions(  # noqa: SLF001
                second, picker, _snapshot(("new.py",), root_b)
            )
            app._install_file_suggestions(  # noqa: SLF001
                first, picker, _snapshot(("stale.py",), root_a)
            )
            return picker.snapshot.root if picker.snapshot else None, "stale.py" in (
                picker.snapshot.paths if picker.snapshot else ()
            )

    root, stale_visible = anyio.run(scenario)
    assert root == root_b.resolve()
    assert stale_visible is False


def test_cwd_transition_invalidates_before_worker_canonicalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> tuple[bool, str]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)):
            picker = app.query_one("#file-suggest", FileSuggest)
            pending: list[FileIndexRequest] = []
            app._start_file_index_request = (  # type: ignore[method-assign]  # noqa: SLF001
                lambda request, _picker: pending.append(request)
            )
            first = app.load_file_suggestions(str(tmp_path / "old"))
            assert first is not None
            app._install_file_suggestions(  # noqa: SLF001
                first, picker, _snapshot(("old.py",), tmp_path / "old")
            )

            def fail_resolve(*_args: object, **_kwargs: object) -> Path:
                raise OSError("resolution belongs to the worker")

            monkeypatch.setattr(Path, "resolve", fail_resolve)
            second = app.load_file_suggestions(str(tmp_path / "new"))
            assert second is not None
            return picker.snapshot is None, second.cwd

    invalidated, raw_cwd = anyio.run(scenario)
    assert invalidated is True
    assert raw_cwd == str(tmp_path / "new")


def test_auth_transition_invalidates_before_worker_canonicalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> tuple[bool, tuple[str, ...]]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)):
            picker = app.query_one("#file-suggest", FileSuggest)
            picker.set_snapshot(_snapshot(("auth.json",), tmp_path))

            def fail_resolve(*_args: object, **_kwargs: object) -> Path:
                raise OSError("resolution belongs to the worker")

            monkeypatch.setattr(Path, "resolve", fail_resolve)
            raw_auth = tmp_path / "auth.json"
            app.set_picker_auth_path(raw_auth)
            return picker.snapshot is None, app._adopted_auth_paths  # noqa: SLF001

    invalidated, adopted = anyio.run(scenario)
    assert invalidated is True
    assert adopted == (str(tmp_path / "auth.json"),)


def test_auth_transition_invalidates_and_rejects_old_completion(tmp_path: Path) -> None:
    async def scenario() -> tuple[tuple[str, ...], bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)):
            picker = app.query_one("#file-suggest", FileSuggest)
            app._start_file_index_request = lambda *_args: None  # type: ignore[method-assign]  # noqa: SLF001,E501
            first = app.load_file_suggestions(str(tmp_path))
            assert first is not None
            app._install_file_suggestions(  # noqa: SLF001
                first, picker, _snapshot(("app.py", "auth.json"), tmp_path)
            )
            picker.show_for("@auth", 5)
            assert picker.is_open is True

            app.set_picker_auth_path(tmp_path / "auth.json")
            invalidated_before_worker = (
                picker.snapshot is None
                and picker.selected_path is None
                and picker.visible_paths == ()
            )
            second = app.load_file_suggestions(str(tmp_path))
            assert second is not None
            app._install_file_suggestions(  # noqa: SLF001
                first, picker, _snapshot(("stale-auth.json",), tmp_path)
            )
            app._install_file_suggestions(  # noqa: SLF001
                second, picker, _snapshot(("app.py",), tmp_path)
            )
            return picker.snapshot.paths if picker.snapshot else (), invalidated_before_worker

    paths, invalidated = anyio.run(scenario)
    assert invalidated is True
    assert paths == ("app.py",)


def test_mention_sessions_refresh_once_and_reject_reverse_reopen(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, tuple[str, ...], bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one("#file-suggest", FileSuggest)
            pending: list[FileIndexRequest] = []
            app._start_file_index_request = (  # type: ignore[method-assign]  # noqa: SLF001
                lambda request, _picker: pending.append(request)
            )
            initial = app.load_file_suggestions(str(tmp_path))
            assert initial is not None
            app._install_file_suggestions(  # noqa: SLF001
                initial, picker, _snapshot(("old.py",), tmp_path)
            )

            await pilot.press("@")
            await pilot.press("o", "l", "d")
            await pilot.pause()
            old_visible_during_refresh = picker.snapshot is not None and picker.is_open
            assert len(pending) == 2  # initial load plus one refresh, not one per key.
            first_reopen = pending[-1]

            await pilot.press("space", "@")
            await pilot.pause()
            assert len(pending) == 3
            second_reopen = pending[-1]
            app._install_file_suggestions(  # noqa: SLF001
                second_reopen, picker, _snapshot(("new.py",), tmp_path)
            )
            app._install_file_suggestions(  # noqa: SLF001
                first_reopen, picker, _snapshot(("stale.py",), tmp_path)
            )
            return (
                len(pending),
                picker.snapshot.paths if picker.snapshot else (),
                old_visible_during_refresh,
            )

    requests, paths, old_visible = anyio.run(scenario)
    assert requests == 3
    assert paths == ("new.py",)
    assert old_visible is True


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
            return picker.visible_paths

    options = anyio.run(scenario)
    assert "app.py" in options
    assert ".env" not in options
