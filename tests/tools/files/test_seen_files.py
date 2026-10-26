"""write and edit refuse to change a file the model has not seen in its current form."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch

from tests.tools.support import run_tool
from wisp.tools.base import Tool
from wisp.tools.builtin import EditTool, ReadTool, WriteTool
from wisp.tools.context import ToolContext
from wisp.tools.files import operations as operations_module
from wisp.tools.result import ToolError
from wisp.tools.seen_files import SeenFiles

_CHANGES = [
    pytest.param(WriteTool(), {"path": "a.txt", "content": "new\n"}, id="write"),
    pytest.param(
        EditTool(), {"path": "a.txt", "edits": [{"oldText": "old", "newText": "new"}]}, id="edit"
    ),
]


def _context(cwd: Path) -> ToolContext:
    return ToolContext(cwd=cwd, seen_files=SeenFiles())


@pytest.mark.parametrize(("tool", "arguments"), _CHANGES)
def test_refuses_unread_file(tmp_path: Path, tool: Tool, arguments: dict[str, object]) -> None:
    (tmp_path / "a.txt").write_text("old\n", encoding="utf-8")

    with pytest.raises(ToolError, match="Read a.txt before changing it") as caught:
        run_tool(tool, arguments, _context(tmp_path))

    assert caught.value.failure_code == "stale_input"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize(("tool", "arguments"), _CHANGES)
def test_refuses_file_changed_since_read(
    tmp_path: Path, tool: Tool, arguments: dict[str, object]
) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old\n", encoding="utf-8")
    context = _context(tmp_path)
    run_tool(ReadTool(), {"path": "a.txt"}, context)
    # The user edits the file outside the model's view; "old" still matches.
    target.write_text("old\nuser line\n", encoding="utf-8")

    with pytest.raises(ToolError, match="a.txt changed since it was last read"):
        run_tool(tool, arguments, context)

    assert target.read_text(encoding="utf-8") == "old\nuser line\n"


def test_own_changes_stay_current(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("one\n", encoding="utf-8")
    context = _context(tmp_path)

    run_tool(ReadTool(), {"path": "a.txt", "limit": 1}, context)
    run_tool(
        EditTool(), {"path": "a.txt", "edits": [{"oldText": "one", "newText": "two"}]}, context
    )
    run_tool(
        EditTool(), {"path": "a.txt", "edits": [{"oldText": "two", "newText": "three"}]}, context
    )
    run_tool(WriteTool(), {"path": "a.txt", "content": "four\n"}, context)

    assert target.read_text(encoding="utf-8") == "four\n"


def test_new_file_needs_no_read(tmp_path: Path) -> None:
    run_tool(WriteTool(), {"path": "new.txt", "content": "x\n"}, _context(tmp_path))

    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "x\n"


@pytest.mark.skipif(os.name != "posix", reason="symbolic links")
def test_read_through_link_counts_for_target(tmp_path: Path) -> None:
    (tmp_path / "target.txt").write_text("old\n", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to("target.txt")
    context = _context(tmp_path)

    run_tool(ReadTool(), {"path": "link.txt"}, context)
    run_tool(
        EditTool(),
        {"path": "target.txt", "edits": [{"oldText": "old", "newText": "new"}]},
        context,
    )

    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "new\n"


def test_failed_read_does_not_count(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("old\n", encoding="utf-8")
    context = _context(tmp_path)
    with pytest.raises(ToolError, match="beyond the end"):
        run_tool(ReadTool(), {"path": "a.txt", "offset": 5}, context)

    with pytest.raises(ToolError, match="Read a.txt before changing it"):
        run_tool(WriteTool(), {"path": "a.txt", "content": "new\n"}, context)


def _rewrite_in_place(path: Path) -> None:
    with path.open("r+", encoding="utf-8") as file:
        file.seek(0, os.SEEK_END)
        file.write("user line\n")


def test_read_during_rewrite_does_not_count(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old\n", encoding="utf-8")
    original = operations_module._read_line_slice  # noqa: SLF001

    def read_then_rewrite(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        _rewrite_in_place(target)
        return result

    monkeypatch.setattr(operations_module, "_read_line_slice", read_then_rewrite)
    context = _context(tmp_path)
    run_tool(ReadTool(), {"path": "a.txt"}, context)

    with pytest.raises(ToolError, match="Read a.txt before changing it"):
        run_tool(WriteTool(), {"path": "a.txt", "content": "new\n"}, context)


@pytest.mark.skipif(os.name != "posix", reason="POSIX publish by rename")
@pytest.mark.parametrize(("tool", "arguments"), _CHANGES)
def test_change_after_publish_is_not_ours(
    tmp_path: Path, monkeypatch: MonkeyPatch, tool: Tool, arguments: dict[str, object]
) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old\n", encoding="utf-8")
    context = _context(tmp_path)
    run_tool(ReadTool(), {"path": "a.txt"}, context)
    cleanup = operations_module._cleanup_temporary  # noqa: SLF001

    def cleanup_then_rewrite(*args: Any, **kwargs: Any) -> None:
        cleanup(*args, **kwargs)
        # Another process changes the file right after it was published.
        _rewrite_in_place(target)

    monkeypatch.setattr(operations_module, "_cleanup_temporary", cleanup_then_rewrite)
    run_tool(tool, arguments, context)
    monkeypatch.setattr(operations_module, "_cleanup_temporary", cleanup)

    # The record still holds the version the model read, not the rewritten file.
    with pytest.raises(ToolError, match="a.txt changed since it was last read"):
        run_tool(WriteTool(), {"path": "a.txt", "content": "newer\n"}, context)
    assert target.read_text(encoding="utf-8").endswith("user line\n")
