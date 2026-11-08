"""Every shipped task's verifier rejects the untouched fixture and accepts its solution."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from evals.tool_use.runner import _prepare, _run_command, command_environment
from evals.tool_use.task_definitions import Task, select_tasks

_TASKS = select_tasks()


def _verify(task: Task, tmp_path: Path, *, solved: bool) -> int | None:
    work = tmp_path / "work"
    home = tmp_path / "home"
    home.mkdir(parents=True)
    shutil.copytree(task.fixture, work)
    environment = command_environment(home, os.environ)
    assert _prepare(task, work, tmp_path, environment, sys.executable) is None
    if solved:
        shutil.copytree(task.folder / "solution", work, dirs_exist_ok=True)
    return _run_command(
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


def test_typographic_fixture_keeps_bytes() -> None:
    fixture = next(task for task in _TASKS if task.id == "edit-typographic").fixture
    text = (fixture / "notices.py").read_bytes()

    assert b"\xc2\xa0" in text and b"\xe2\x80\x9c" in text


def test_bom_fixture_keeps_mark() -> None:
    fixture = next(task for task in _TASKS if task.id == "edit-bom-file").fixture
    assert (fixture / "messages.py").read_bytes().startswith(b"\xef\xbb\xbf")
