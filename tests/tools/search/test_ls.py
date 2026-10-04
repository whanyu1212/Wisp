from __future__ import annotations

import threading
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from tests.tools.support import run_tool
from wisp.tools.builtin import LsTool
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolResult
from wisp.tools.search import tools as search_tools_module

pytestmark = pytest.mark.process


def test_ls_tool_lists_sorted_entries_with_directory_suffix(tmp_path: Path) -> None:
    (tmp_path / "zeta.txt").write_text("", encoding="utf-8")
    (tmp_path / "alpha").mkdir()
    (tmp_path / ".hidden").write_text("", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(LsTool(), {"path": "."}, context)

    assert result.text == "alpha/\nzeta.txt"
    assert result.data["entries"] == ["alpha/", "zeta.txt"]


def test_ls_tool_truncates_large_output(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("", encoding="utf-8")
    (tmp_path / "b.txt").write_text("", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_output_bytes=100, max_output_lines=1)

    result = run_tool(LsTool(), {"path": "."}, context)

    assert result.text == "a.txt\n[truncated]"
    assert result.truncated is True


def test_ls_tool_bounds_retained_entries_and_reports_exact_count(tmp_path: Path) -> None:
    for index in range(20):
        (tmp_path / f"entry-{index:02}.txt").write_text("", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_output_bytes=100, max_output_lines=2)

    result = run_tool(LsTool(), {"path": "."}, context)

    assert result.text == "entry-00.txt\nentry-01.txt\n[truncated]"
    assert result.data == {
        "path": ".",
        "entries": ["entry-00.txt", "entry-01.txt"],
        "entry_count": 20,
    }
    assert result.truncated is True


def test_ls_tool_preserves_case_insensitive_order_and_hidden_option(tmp_path: Path) -> None:
    for name in ("a", "C", "b", ".hidden"):
        (tmp_path / name).write_text("", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    visible = run_tool(LsTool(), {"path": "."}, context)
    all_entries = run_tool(LsTool(), {"path": ".", "all": True}, context)

    assert visible.data["entries"] == ["a", "b", "C"]
    assert visible.data["entry_count"] == 3
    assert all_entries.data["entries"] == [".hidden", "a", "b", "C"]
    assert all_entries.data["entry_count"] == 4


def test_ls_tool_runs_off_event_loop_and_abandons_worker_wait_on_cancel(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking_ls(**_kwargs: object) -> ToolResult:
        started.set()
        release.wait(timeout=2)
        return ToolResult(text="", data={"entries": [], "entry_count": 0})

    monkeypatch.setattr(search_tools_module, "_python_ls", blocking_ls)

    async def scenario() -> None:
        with anyio.fail_after(0.5):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(LsTool().run, {}, ToolContext(cwd=tmp_path))
                assert await anyio.to_thread.run_sync(started.wait, 1)
                await anyio.sleep(0)
                task_group.cancel_scope.cancel()
        release.set()

    anyio.run(scenario)
