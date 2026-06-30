from __future__ import annotations

import shlex
import shutil
import sys
import time
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from wisp.tools.builtin import BashTool, EditTool, FindTool, GrepTool, LsTool, ReadTool, WriteTool
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError, ToolResult


def run_tool(tool: object, arguments: dict[str, object], context: ToolContext) -> ToolResult:
    async def run() -> ToolResult:
        result = await tool.run(arguments, context)  # type: ignore[attr-defined]
        assert isinstance(result, ToolResult)
        return result

    return anyio.run(run)


def test_read_tool_supports_offset_limit_and_truncation(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_output_bytes=100, max_output_lines=2)

    result = run_tool(ReadTool(), {"path": "notes.txt", "offset": 2, "limit": 3}, context)

    assert result.text == "two\nthree\n[truncated]"
    assert result.truncated is True
    assert result.data["line_count"] == 4


def test_write_tool_creates_parent_directories_and_overwrites(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)

    first = run_tool(WriteTool(), {"path": "nested/file.txt", "content": "first"}, context)
    second = run_tool(WriteTool(), {"path": "nested/file.txt", "content": "second"}, context)

    assert first.data["bytes"] == 5
    assert second.data["bytes"] == 6
    assert (tmp_path / "nested/file.txt").read_text(encoding="utf-8") == "second"


def test_edit_tool_applies_unique_replacements_from_original(tmp_path: Path) -> None:
    path = tmp_path / "story.txt"
    path.write_text("hello brave world\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        EditTool(),
        {
            "path": "story.txt",
            "edits": [
                {"oldText": "hello", "newText": "hi"},
                {"oldText": "world", "newText": "Wisp"},
            ],
        },
        context,
    )

    assert result.data["edits"] == 2
    assert path.read_text(encoding="utf-8") == "hi brave Wisp\n"


def test_edit_tool_rejects_non_unique_replacement(tmp_path: Path) -> None:
    path = tmp_path / "dupes.txt"
    path.write_text("same same\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    with pytest.raises(ToolError, match="found 2 matches"):
        run_tool(
            EditTool(),
            {"path": "dupes.txt", "edits": [{"oldText": "same", "newText": "once"}]},
            context,
        )


def test_bash_tool_captures_stdout_stderr_and_exit_code(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)
    python = shlex.quote(sys.executable)
    command = (
        f"{python} -c \"import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)\""
    )

    result = run_tool(BashTool(), {"command": command}, context)

    assert result.data["exit_code"] == 3
    assert result.data["stdout"] == "out\n"
    assert result.data["stderr"] == "err\n"
    assert "out" in result.text
    assert "err" in result.text


def test_bash_tool_reports_timeout_and_kills_child_processes(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)
    python = shlex.quote(sys.executable)
    marker = tmp_path / "child-survived.txt"
    child_code = (
        f"import pathlib, time; time.sleep(1.5); pathlib.Path({str(marker)!r}).write_text('alive')"
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(5)"
    )
    command = f"{python} -c {shlex.quote(parent_code)}"

    with pytest.raises(ToolError, match="timed out"):
        run_tool(BashTool(), {"command": command, "timeout": 1}, context)
    time.sleep(1.0)

    assert not marker.exists()


def test_grep_tool_python_fallback_supports_literal_ignore_case_and_glob(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "alpha.txt").write_text("Needle here\n", encoding="utf-8")
    (tmp_path / "beta.md").write_text("needle elsewhere\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {
            "pattern": "needle",
            "path": ".",
            "glob": "*.txt",
            "literal": True,
            "ignore_case": True,
        },
        context,
    )

    assert result.data["count"] == 1
    assert result.text == "alpha.txt:1:Needle here"


def test_grep_tool_ripgrep_treats_option_like_pattern_as_literal(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    (tmp_path / "data.txt").write_text("--help\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "--help", "path": ".", "literal": True},
        context,
    )

    assert result.text == "data.txt:1:--help"
    assert "Usage:" not in result.text


def test_find_tool_python_fallback_filters_glob(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "one.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "two.txt").write_text("", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(FindTool(), {"path": ".", "pattern": "*.py"}, context)

    assert result.text == "pkg/one.py"
    assert result.data["files"] == ["pkg/one.py"]


def test_find_tool_ripgrep_handles_option_like_paths(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    dash_dir = tmp_path / "-dash"
    dash_dir.mkdir()
    (dash_dir / "tool.py").write_text("", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(FindTool(), {"path": "-dash", "pattern": "*.py"}, context)

    assert result.text == "-dash/tool.py"


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
