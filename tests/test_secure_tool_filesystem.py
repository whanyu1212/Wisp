from __future__ import annotations

import errno
import os
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from wisp.tools import file_ops as file_ops_module
from wisp.tools import secure_fs as secure_fs_module
from wisp.tools.context import ToolContext
from wisp.tools.file_ops import EditTool, ReadTool, WriteTool
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.search import FindTool, GrepTool, LsTool


def run_tool(tool: object, arguments: dict[str, object], context: ToolContext) -> ToolResult:
    async def run() -> ToolResult:
        return await tool.run(arguments, context)  # type: ignore[attr-defined]

    return anyio.run(run)


def test_read_rejects_final_symlink_even_when_outside_access_is_allowed(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("secret\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    with pytest.raises(ToolError, match="Symbolic links are not allowed"):
        run_tool(
            ReadTool(), {"path": "link.txt"}, ToolContext(cwd=tmp_path, allow_outside_cwd=True)
        )


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="requires no-follow descriptors")
def test_read_rejects_ancestor_replaced_with_symlink(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    safe = workspace / "safe"
    safe.mkdir(parents=True)
    (safe / "data.txt").write_text("ordinary\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.txt").write_text("secret\n", encoding="utf-8")
    real_open_child = secure_fs_module._open_child_directory
    swapped = False

    def swap_then_open(parent_fd: int, name: str, *, display: str) -> int:
        nonlocal swapped
        if name == "safe" and not swapped:
            swapped = True
            safe.rename(workspace / "safe-original")
            safe.symlink_to(outside, target_is_directory=True)
        return real_open_child(parent_fd, name, display=display)

    monkeypatch.setattr(secure_fs_module, "_open_child_directory", swap_then_open)

    with pytest.raises(ToolError, match="symbolic link|non-directory"):
        run_tool(ReadTool(), {"path": "safe/data.txt"}, ToolContext(cwd=workspace))


def test_overwrite_publish_failure_preserves_original(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("original\n", encoding="utf-8")

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(file_ops_module.os, "replace", fail_replace)

    with pytest.raises(ToolError, match="Could not create file"):
        run_tool(
            WriteTool(),
            {"path": "target.txt", "content": "replacement\n"},
            ToolContext(cwd=tmp_path),
        )

    assert target.read_text(encoding="utf-8") == "original\n"
    assert not list(tmp_path.glob(".wisp-write-*"))


def test_overwrite_preserves_existing_permission_bits(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("original\n", encoding="utf-8")
    target.chmod(0o640)

    run_tool(
        WriteTool(),
        {"path": "target.txt", "content": "replacement\n"},
        ToolContext(cwd=tmp_path),
    )

    assert target.stat().st_mode & 0o777 == 0o640


def test_overwrite_rejects_final_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("original\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    with pytest.raises(ToolError, match="Symbolic links are not allowed"):
        run_tool(
            WriteTool(),
            {"path": "link.txt", "content": "replacement\n"},
            ToolContext(cwd=tmp_path),
        )

    assert target.read_text(encoding="utf-8") == "original\n"


def test_write_allows_missing_conflict_parent(tmp_path: Path) -> None:
    context = ToolContext(
        cwd=tmp_path,
        conflicting_write_paths=(Path("nested/AGENTS.MD"),),
    )

    run_tool(
        WriteTool(),
        {"path": "nested/AGENTS.md", "content": "guidance\n", "overwrite": False},
        context,
    )

    assert (tmp_path / "nested/AGENTS.md").read_text(encoding="utf-8") == "guidance\n"


def test_edit_rejects_destination_replaced_before_publish(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("original\n", encoding="utf-8")
    real_stat_leaf = file_ops_module.stat_leaf
    calls = 0

    def replace_before_second_stat(parent: object) -> os.stat_result | None:
        nonlocal calls
        calls += 1
        if calls == 2:
            target.unlink()
            target.write_text("concurrent\n", encoding="utf-8")
        return real_stat_leaf(parent)  # type: ignore[arg-type]

    monkeypatch.setattr(file_ops_module, "stat_leaf", replace_before_second_stat)

    with pytest.raises(ToolError, match="File changed while editing"):
        run_tool(
            EditTool(),
            {"path": "target.txt", "edits": [{"oldText": "original", "newText": "edited"}]},
            ToolContext(cwd=tmp_path),
        )

    assert target.read_text(encoding="utf-8") == "concurrent\n"
    assert not list(tmp_path.glob(".wisp-write-*"))


def test_recursive_tools_skip_directory_symlinks_even_when_opted_out(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("needle\n", encoding="utf-8")
    (workspace / "linked").symlink_to(outside, target_is_directory=True)
    context = ToolContext(cwd=workspace, allow_outside_cwd=True)

    result = run_tool(GrepTool(), {"path": ".", "pattern": "needle"}, context)

    assert result.text == "No matches"


def test_recursive_tools_honor_repository_ignore_files(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("generated/\n", encoding="utf-8")
    (tmp_path / ".ignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / ".rgignore").write_text("rg-only.py\n", encoding="utf-8")
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "match.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "rg-only.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "visible.py").write_text("needle\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    grep = run_tool(GrepTool(), {"path": ".", "pattern": "needle"}, context)
    find = run_tool(FindTool(), {"path": ".", "pattern": "*.py"}, context)

    assert grep.data["matches"] == ["visible.py:1:needle"]
    assert find.data["files"] == ["visible.py"]


def test_regex_search_times_out_pathological_backtracking(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text(("a" * 200_000) + "!\n", encoding="utf-8")

    with pytest.raises(ToolError, match="regex evaluation time limit"):
        run_tool(
            GrepTool(),
            {"path": ".", "pattern": "(a+)+$"},
            ToolContext(cwd=tmp_path),
        )


def test_ls_displays_but_does_not_follow_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    link = workspace / "linked"
    link.symlink_to(outside, target_is_directory=True)

    listing = run_tool(LsTool(), {"path": "."}, ToolContext(cwd=workspace))
    assert listing.text == "linked"

    with pytest.raises(ToolError, match="symbolic link|non-directory"):
        run_tool(LsTool(), {"path": "linked"}, ToolContext(cwd=workspace))
