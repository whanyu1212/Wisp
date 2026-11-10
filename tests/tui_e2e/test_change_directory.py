"""`/cd` in the Rust TUI against the real backend: completion, the move, and resume."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.tui_e2e.support import run_settled, screen_shows
from tests.tui_e2e.test_readiness import _launch, _write_user_settings


def _saved_moves(session_dir: Path) -> list[dict[str, object]]:
    return [
        entry["event"]["payload"]
        for path in session_dir.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
        if (entry := json.loads(line)).get("kind") == "event"
        and entry["event"]["payload"].get("type") == "session.directory_changed"
    ]


@pytest.mark.process
def test_cd_moves_and_resumes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "README.md").write_text("# Fixture project\n", encoding="utf-8")
    _write_user_settings(home, provider="fake")
    tui, session_dir = _launch(tmp_path, home=home, cwd=project)
    try:
        tui.wait_ready()
        offset = len(tui.output)
        tui.send(b"hello\r")
        tui.wait_until(
            lambda output: run_settled(output, offset, b"fake", b"response"),
            failure="Rust TUI did not finish the first prompt",
        )

        # The folder completes from the local file system; Tab fills it in.
        tui.send(b"/cd s")
        tui.wait_until(
            lambda output: screen_shows(output, "src/"),
            failure="Rust TUI did not offer the project's folder",
        )
        tui.send(b"\t")
        tui.wait_until(
            lambda output: screen_shows(output, "/cd src/"),
            failure="Rust TUI did not fill in the folder",
        )
        tui.submit(
            lambda output: screen_shows(output, "── Working in"),
            failure="Rust TUI did not draw the move's divider",
        )
        tui.quit()
    finally:
        tui.close()

    (move,) = _saved_moves(session_dir)
    assert Path(str(move["cwd"])) == (project / "src").resolve()
    (session_file,) = session_dir.glob("*.jsonl")
    session_id = json.loads(session_file.read_text(encoding="utf-8").splitlines()[0])["session_id"]

    # Resuming the session works from its folder and redraws the divider from history.
    tui, _session_dir = _launch(tmp_path, home=home, cwd=project)
    try:
        tui.wait_ready()
        tui.send(f"/resume {session_id}".encode())
        tui.submit(
            lambda output: screen_shows(output, "── Working in") and screen_shows(output, "src"),
            failure="Rust TUI did not restore the move from history",
        )
        tui.quit()
    finally:
        tui.close()
