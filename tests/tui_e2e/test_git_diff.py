"""Dedicated Git review through the real Python/Rust handoff."""

import subprocess
from pathlib import Path

import pytest

from tests.tui_e2e.support import rendered_without_spacing
from tests.tui_e2e.test_readiness import _launch, _write_user_settings


@pytest.mark.process
def test_diff_opens_refreshes_and_returns_to_composer(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(project), *args], check=True, capture_output=True)

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (project / "example.py").write_text("original\n")
    git("add", "example.py")
    git("commit", "-m", "initial")
    (project / "example.py").write_text("updated\n")
    home = tmp_path / "home"
    _write_user_settings(home, provider="fake")
    tui, _ = _launch(tmp_path, home=home, cwd=project)
    try:
        tui.wait_ready()
        tui.send(b"/diff\r")
        tui.wait_until(
            lambda out: b"example.py" in rendered_without_spacing(out),
            failure="diff file did not render",
        )
        tui.send(b"\t")
        tui.wait_until(
            lambda out: b"updated" in rendered_without_spacing(out),
            failure="working file diff did not render",
        )
        (project / "new_file.txt").write_text("untracked\n")
        offset = len(tui.output)
        tui.send(b"r")
        tui.wait_until(
            lambda out: b"new_file.txt" in rendered_without_spacing(out[offset:]),
            failure="refresh did not discover untracked file",
        )
        tui.send(b"\x1b")
        tui.wait_until(
            lambda out: b"Enter" in rendered_without_spacing(out), failure="composer did not return"
        )
        tui.send(b"after diff")
        tui.wait_for(b"after diff", failure="diff retained composer focus")
        tui.quit()
    finally:
        tui.close()
