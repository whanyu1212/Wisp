"""Loading eval tasks from `evals/tool_use/tasks/` (`evals.tool_use.task_definitions`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.tool_use.task_definitions import TaskError, load_task, select_tasks

_VALID = """
id = "{id}"
title = "A task"
tags = ["general"]
prompt = "Do it"
verify = "true"
tools = ["read"]
"""


def _task(root: Path, folder: str, text: str) -> Path:
    path = root / folder
    (path / "repo").mkdir(parents=True)
    (path / "task.toml").write_text(text, encoding="utf-8")
    return path


def test_shipped_tasks_load() -> None:
    tasks = select_tasks()

    assert {task.id for task in tasks} >= {
        "early-build-error",
        "edit-bom-file",
        "fix-failing-test",
    }
    assert all(task.fixture.is_dir() for task in tasks)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (_VALID.format(id="other"), "must match its folder name"),
        (_VALID.format(id="demo") + 'surprise = "x"\n', "Extra inputs"),
        (_VALID.format(id="demo").replace('["read"]', '["shell"]'), "tools"),
        (_VALID.format(id="demo") + 'mcp_servers = ["nope"]\n', "unknown MCP servers nope"),
        ("id = ", "invalid"),
    ],
    ids=["id-mismatch", "unknown-key", "unknown-tool", "unknown-server", "bad-toml"],
)
def test_bad_task_rejected(tmp_path: Path, text: str, message: str) -> None:
    folder = _task(tmp_path, "demo", text)

    with pytest.raises(TaskError, match=message):
        load_task(folder)


def test_selects_by_tag_or_id(tmp_path: Path) -> None:
    _task(tmp_path, "one", _VALID.format(id="one"))
    _task(tmp_path, "two", _VALID.format(id="two").replace('["general"]', '["edit"]'))

    assert [task.id for task in select_tasks(tmp_path, tags=["edit"])] == ["two"]
    assert [task.id for task in select_tasks(tmp_path, ids=["one"])] == ["one"]
    with pytest.raises(TaskError, match="no task tagged mcp"):
        select_tasks(tmp_path, tags=["mcp"])
