"""Every shipped task's verifier rejects the untouched fixture and accepts its solution."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from evals.tool_use.runner import _prepare, command_environment, run_task_command
from evals.tool_use.task_definitions import TASKS_DIR, Task, load_task

# Every task folder, opt-in ones too, which a whole-suite selection leaves out.
_TASKS = tuple(load_task(folder) for folder in sorted(TASKS_DIR.iterdir()) if folder.is_dir())


def _verify(task: Task, tmp_path: Path, *, solved: bool, with_home: bool = True) -> int | None:
    """Run a task's verifier on its fixture, optionally with the solution applied.

    A solved run copies `solution/` into the work folder and, unless ``with_home`` is
    false, `solution-home/` into `HOME`, for what a solved run leaves there.
    """

    work = tmp_path / "work"
    home = tmp_path / "home"
    home.mkdir(parents=True)
    shutil.copytree(task.fixture, work)
    environment = command_environment(home, os.environ)
    assert _prepare(task, work, tmp_path, environment, sys.executable) is None
    if solved:
        shutil.copytree(task.folder / "solution", work, dirs_exist_ok=True)
    home_files = task.folder / "solution-home"
    if solved and with_home and home_files.is_dir():
        shutil.copytree(home_files, home, dirs_exist_ok=True)
    return run_task_command(
        task.definition.verify,
        work,
        environment,
        sys.executable,
        task.folder,
        tmp_path / "verify.txt",
    )


@pytest.mark.process
@pytest.mark.parametrize("task", _TASKS, ids=[task.id for task in _TASKS])
def test_verifier_separates_solution(task: Task, tmp_path: Path) -> None:
    assert (task.folder / "solution").is_dir(), "every task ships a reference solution"
    assert _verify(task, tmp_path / "untouched", solved=False) != 0
    assert _verify(task, tmp_path / "solved", solved=True) == 0


@pytest.mark.process
def test_costly_rerun_needs_one_report(tmp_path: Path) -> None:
    task = next(task for task in _TASKS if task.id == "costly-rerun")

    assert _verify(task, tmp_path / "skipped", solved=True, with_home=False) != 0
    assert _verify(task, tmp_path / "once", solved=True) == 0


def test_typographic_fixture_keeps_bytes() -> None:
    fixture = next(task for task in _TASKS if task.id == "edit-typographic").fixture
    text = (fixture / "notices.py").read_bytes()

    assert b"\xc2\xa0" in text and b"\xe2\x80\x9c" in text


def test_bom_fixture_keeps_mark() -> None:
    fixture = next(task for task in _TASKS if task.id == "edit-bom-file").fixture
    assert (fixture / "messages.py").read_bytes().startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize(
    ("answer", "passes"),
    [
        ("issue: 412\npr: 418\ncheck: e2e-safari\n", True),
        ("- issue: `#412`\n- pr: #418\n- check: `E2E-Safari`\n", True),
        ("issue: 405\npr: 418\ncheck: e2e-safari\n", False),
        ("issue: 420\npr: 418\ncheck: e2e-safari\n", False),
        ("issue: 412\npr: 421\ncheck: e2e-chrome\n", False),
        ("issue: 412\npr: 409\ncheck: e2e-safari\n", False),
    ],
    ids=["exact", "formatted", "closed-issue", "not-regression", "wrong-pr", "closed-pr"],
)
def test_triage_verifier(tmp_path: Path, answer: str, passes: bool) -> None:
    task = next(task for task in _TASKS if task.id == "mcp-triage")
    (tmp_path / "TRIAGE.md").write_text(answer, encoding="utf-8")

    exit_code = subprocess.run(
        [sys.executable, str(task.folder / "verify.py")], cwd=tmp_path, check=False
    ).returncode

    assert (exit_code == 0) is passes
