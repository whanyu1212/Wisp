"""Lazy Files browsing and project search through the Python/Rust handoff."""

from pathlib import Path

import pytest

from tests.tui_e2e.support import complete_frames, display_width, screen_rows, screen_shows
from tests.tui_e2e.test_readiness import _launch, _write_user_settings


@pytest.mark.process
def test_browse_and_search_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "alpha").mkdir(parents=True)
    (project / "zeta" / "nested").mkdir(parents=True)
    (project / "README.md").write_text("# Test project\n", encoding="utf-8")
    (project / "alpha" / "first-file.rs").touch()
    (project / "zeta" / "nested" / "search-hit.rs").touch()
    home = tmp_path / "home"
    _write_user_settings(home, provider="fake")

    tui, _ = _launch(tmp_path, home=home, cwd=project, env={"WISP_TUI_MOUSE": "1"})

    def click_label(output: bytes, label: str) -> None:
        for row, text in enumerate(screen_rows(complete_frames(output)), 1):
            if label in text:
                column = display_width(text[: text.index(label)]) + 1
                tui.send(f"\x1b[<0;{column};{row}M\x1b[<0;{column};{row}m".encode())
                return
        pytest.fail(f"Cannot click missing label {label!r}")

    try:
        tui.wait_ready()
        tui.resize(width=152)
        # The startup frame may already show the tabs at the old coordinates.
        output = tui.wait_until(
            lambda out: any(
                "Session   Files   Browser" in row and display_width(row) > 101
                for row in screen_rows(complete_frames(out))
            ),
            failure="The docked side panel did not render",
        )
        click_label(output, "Files")
        output = tui.wait_until(
            lambda out: screen_shows(out, "Project tree") and screen_shows(out, "README.md"),
            failure="Files root listing did not appear",
        )
        assert not screen_shows(output, "first-file.rs")
        assert not screen_shows(output, "search-hit.rs")

        click_label(output, "alpha/")
        tui.wait_until(
            lambda out: screen_shows(out, "first-file.rs"),
            failure="Expanding a folder did not load its children",
        )
        tui.send(b"/search-hit\r")
        tui.wait_until(
            lambda out: screen_shows(out, "search-hit.rs"),
            failure="Search did not find a file in an unopened folder",
        )

        tui.send(b"\x1b")
        tui.wait_until(
            lambda out: (
                screen_shows(out, "first-file.rs") and not screen_shows(out, "search-hit.rs")
            ),
            failure="Clearing search did not restore the expanded tree",
        )
        (project / "now-visible.txt").touch()
        tui.send(b"r")
        tui.wait_until(
            lambda out: screen_shows(out, "now-visible.txt") and screen_shows(out, "first-file.rs"),
            failure="Refresh did not update the root and preserve the expansion",
        )
        tui.send(b"\x1b")
        tui.wait_until(
            lambda out: screen_shows(out, "F8 focus"),
            failure="Files retained keyboard focus",
        )
        tui.send(b"work continues")
        tui.wait_until(
            lambda out: screen_shows(out, "work continues"),
            failure="The composer did not accept input with Files open",
        )
        tui.quit()
    finally:
        tui.close()
