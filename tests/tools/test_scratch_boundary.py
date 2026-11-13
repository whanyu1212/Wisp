"""Which paths the file and search tools may reach in the scratch folder."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.tools.support import run_tool
from wisp.tools.builtin import EditTool, FindTool, GrepTool, LsTool, ReadTool, WriteTool
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    """A scratch folder beside a project, holding one note and a secret."""

    (tmp_path / "project").mkdir()
    folder = (tmp_path / "scratch").resolve()
    folder.mkdir()
    (folder / "notes.md").write_text("needle = 9\n", encoding="utf-8")
    (folder / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("needle = 2\n", encoding="utf-8")
    return folder


def _context(scratch: Path, *, granted: bool = True) -> ToolContext:
    return ToolContext(cwd=scratch.parent / "project", scratch_root=scratch if granted else None)


SCRATCH_CALLS = [
    (ReadTool(), {"path": "notes.md"}, "needle = 9"),
    (WriteTool(), {"path": "body.md", "content": "## Summary\n"}, None),
    (
        EditTool(),
        {"path": "notes.md", "edits": [{"oldText": "needle = 9", "newText": "needle = 8"}]},
        None,
    ),
    (GrepTool(), {"pattern": "needle", "path": "."}, "notes.md"),
    (GrepTool(), {"pattern": "needle", "path": "notes.md"}, "needle = 9"),
    (FindTool(), {"path": "."}, "notes.md"),
    (LsTool(), {"path": "."}, "notes.md"),
]
SCRATCH_IDS = ["read", "write", "edit", "grep", "grep-file", "find", "ls"]


def _in_scratch(scratch: Path, arguments: dict[str, object]) -> dict[str, object]:
    return {**arguments, "path": str(scratch / str(arguments["path"]))}


@pytest.mark.parametrize(("tool", "arguments", "expected"), SCRATCH_CALLS, ids=SCRATCH_IDS)
def test_tools_reach_scratch(
    scratch: Path, tool: object, arguments: dict[str, object], expected: str | None
) -> None:
    context = _context(scratch)
    if isinstance(tool, EditTool):
        run_tool(ReadTool(), {"path": str(scratch / "notes.md")}, context)

    result = run_tool(tool, _in_scratch(scratch, arguments), context)

    if expected is not None:
        assert expected in result.text
    if isinstance(tool, WriteTool):
        assert (scratch / "body.md").read_text(encoding="utf-8") == "## Summary\n"
    if isinstance(tool, EditTool):
        assert (scratch / "notes.md").read_text(encoding="utf-8") == "needle = 8\n"


@pytest.mark.parametrize(("tool", "arguments", "expected"), SCRATCH_CALLS, ids=SCRATCH_IDS)
def test_ungranted_scratch_refused(
    scratch: Path, tool: object, arguments: dict[str, object], expected: str | None
) -> None:
    with pytest.raises(ToolError, match="outside the workspace") as raised:
        run_tool(tool, _in_scratch(scratch, arguments), _context(scratch, granted=False))

    assert raised.value.failure_code == "path_outside_workspace"


def test_scratch_neighbour_refused(scratch: Path) -> None:
    """Only the scratch folder opens, not the temp folder around it."""

    with pytest.raises(ToolError, match="outside the workspace") as raised:
        run_tool(ReadTool(), {"path": str(scratch / ".." / "outside.txt")}, _context(scratch))

    assert raised.value.recovery_hint is not None
    assert f"scratch folder {scratch}" in raised.value.recovery_hint


def test_scratch_link_out_refused(scratch: Path) -> None:
    (scratch / "link.txt").symlink_to(scratch.parent / "outside.txt")

    with pytest.raises(ToolError, match="points outside the workspace"):
        run_tool(ReadTool(), {"path": str(scratch / "link.txt")}, _context(scratch))


def test_scratch_secret_protected(scratch: Path) -> None:
    context = _context(scratch)

    with pytest.raises(ToolError, match="protected path"):
        run_tool(ReadTool(), {"path": str(scratch / ".env")}, context)
    result = run_tool(GrepTool(), {"pattern": "TOKEN", "path": str(scratch)}, context)
    assert "secret" not in result.text


def test_project_link_into_scratch_allowed(scratch: Path) -> None:
    """A link from the workspace may point into the scratch folder, as into the workspace."""

    (scratch.parent / "project" / "notes.md").symlink_to(scratch / "notes.md")

    result = run_tool(ReadTool(), {"path": "notes.md"}, _context(scratch))

    assert "needle = 9" in result.text


@pytest.mark.parametrize("broad", ["home", "filesystem-root"])
def test_broad_scratch_grants_nothing(tmp_path: Path, broad: str) -> None:
    root = Path.home() if broad == "home" else Path(Path.home().anchor)

    assert ToolContext(cwd=tmp_path, scratch_root=root).scratch_root is None


def test_config_contexts_get_session_scratch(tmp_path: Path) -> None:
    from wisp.config.runtime import WispConfig

    context = ToolContext.from_config(WispConfig.from_env(), cwd=tmp_path)

    assert context.session_scratch is True
    assert context.scratch_root is None
