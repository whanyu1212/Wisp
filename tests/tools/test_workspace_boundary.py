"""Which paths the file and search tools may reach, from a project subfolder."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.tools.support import run_tool
from wisp.tools.builtin import EditTool, FindTool, GrepTool, LsTool, ReadTool, WriteTool
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project with a `src` working folder, a sibling `tests` folder, and a secret."""

    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text("needle = 1\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("needle = 2\n", encoding="utf-8")
    return root


def _context(root: Path, *, trusted_root: Path | None) -> ToolContext:
    return ToolContext(cwd=root / "src", workspace_root=trusted_root)


def _reads_sibling(root: Path, text: str) -> bool:
    return "needle = 1" in text


def _wrote_sibling(root: Path, text: str) -> bool:
    return (root / "tests" / "new.py").read_text(encoding="utf-8") == "x = 1\n"


def _edited_sibling(root: Path, text: str) -> bool:
    return "needle = 3" in (root / "tests" / "test_app.py").read_text(encoding="utf-8")


def _lists_sibling(root: Path, text: str) -> bool:
    return "test_app.py" in text


SIBLING_CALLS = [
    (ReadTool(), {"path": "../tests/test_app.py"}, _reads_sibling),
    (WriteTool(), {"path": "../tests/new.py", "content": "x = 1\n"}, _wrote_sibling),
    (
        EditTool(),
        {
            "path": "../tests/test_app.py",
            "edits": [{"oldText": "needle = 1", "newText": "needle = 3"}],
        },
        _edited_sibling,
    ),
    (GrepTool(), {"pattern": "needle", "path": "../tests"}, _lists_sibling),
    (FindTool(), {"path": "../tests"}, _lists_sibling),
    (LsTool(), {"path": "../tests"}, _lists_sibling),
]
SIBLING_IDS = ["read", "write", "edit", "grep", "find", "ls"]


@pytest.mark.parametrize(("tool", "arguments", "succeeded"), SIBLING_CALLS, ids=SIBLING_IDS)
def test_root_reaches_sibling_folder(
    project: Path, tool: object, arguments: dict, succeeded: Callable[[Path, str], bool]
) -> None:
    result = run_tool(tool, arguments, _context(project, trusted_root=project))

    assert succeeded(project, result.text)


@pytest.mark.parametrize(("tool", "arguments", "succeeded"), SIBLING_CALLS, ids=SIBLING_IDS)
def test_cwd_boundary_refuses_sibling(
    project: Path, tool: object, arguments: dict, succeeded: Callable[[Path, str], bool]
) -> None:
    """Without a trusted root, the working folder stays the boundary."""

    with pytest.raises(ToolError, match="outside the workspace") as raised:
        run_tool(tool, arguments, _context(project, trusted_root=None))

    assert raised.value.failure_code == "path_outside_workspace"


@pytest.mark.parametrize(
    "path",
    ["../../outside.txt", "../tests/../../outside.txt"],
    ids=["dotdot", "nested-dotdot"],
)
def test_root_refuses_escape(project: Path, path: str) -> None:
    with pytest.raises(ToolError, match="outside the workspace") as raised:
        run_tool(ReadTool(), {"path": path}, _context(project, trusted_root=project))

    assert raised.value.recovery_hint == f"Use a path inside {project.resolve()}."


def test_root_refuses_absolute_escape(project: Path) -> None:
    outside = project.parent / "outside.txt"

    with pytest.raises(ToolError, match="outside the workspace"):
        run_tool(ReadTool(), {"path": str(outside)}, _context(project, trusted_root=project))


@pytest.mark.parametrize(
    ("target", "allowed"),
    [("../tests/test_app.py", True), ("../../outside.txt", False)],
    ids=["inside-root", "outside-root"],
)
def test_link_target_checked_against_root(project: Path, target: str, allowed: bool) -> None:
    (project / "src" / "link.py").symlink_to(target)
    context = _context(project, trusted_root=project)

    if allowed:
        assert run_tool(ReadTool(), {"path": "link.py"}, context).text.startswith("needle = 1")
        return
    with pytest.raises(ToolError, match="points outside the workspace"):
        run_tool(ReadTool(), {"path": "link.py"}, context)


def test_protected_file_above_cwd(project: Path) -> None:
    """Widening the boundary does not expose the root's protected files."""

    with pytest.raises(ToolError, match="protected path"):
        run_tool(ReadTool(), {"path": "../.env"}, _context(project, trusted_root=project))


def test_search_from_root_hides_secret(project: Path) -> None:
    (project / "tests" / "config.py").write_text("TOKEN = None\n", encoding="utf-8")

    result = run_tool(
        GrepTool(), {"pattern": "TOKEN", "path": ".."}, _context(project, trusted_root=project)
    )

    assert "../tests/config.py" in result.text
    assert ".env" not in result.text
    assert "secret" not in result.text


def test_search_above_cwd_honors_ignore(project: Path) -> None:
    """The root's ignore file applies to a search that starts in a sibling folder."""

    (project / ".ignore").write_text("build/\n", encoding="utf-8")
    (project / "tests" / "build").mkdir()
    (project / "tests" / "build" / "out.py").write_text("needle = 4\n", encoding="utf-8")

    result = run_tool(
        GrepTool(),
        {"pattern": "needle", "path": "../tests"},
        _context(project, trusted_root=project),
    )

    assert "test_app.py" in result.text
    assert "out.py" not in result.text


def test_root_without_cwd_never_widens(project: Path, tmp_path: Path) -> None:
    """A workspace root that does not contain the working folder is ignored."""

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    context = ToolContext(cwd=project / "src", workspace_root=elsewhere)

    assert context.boundary() == (project / "src").resolve()
    with pytest.raises(ToolError, match="outside the workspace"):
        run_tool(ReadTool(), {"path": str(elsewhere)}, context)


def test_link_to_protected_file_refused(project: Path) -> None:
    """A link inside the working folder to the root's secret is refused like the secret."""

    (project / "src" / "settings.txt").symlink_to("../.env")

    with pytest.raises(ToolError, match="protected path"):
        run_tool(ReadTool(), {"path": "settings.txt"}, _context(project, trusted_root=project))


@pytest.mark.parametrize("broad", ["home", "filesystem-root"])
def test_broad_root_never_becomes_boundary(tmp_path: Path, broad: str) -> None:
    """A caller-supplied home folder or filesystem root keeps the tools inside cwd."""

    root = Path.home() if broad == "home" else Path(Path.home().anchor)
    cwd = Path.home() / "project"
    cwd.mkdir()

    context = ToolContext(cwd=cwd, workspace_root=root)

    assert context.workspace_root is None
    assert context.boundary() == cwd.resolve()


def test_cwd_search_ignores_unchanged(project: Path) -> None:
    """Trust does not change what a search inside the working folder returns."""

    (project / ".ignore").write_text("gen/\n", encoding="utf-8")
    (project / "src" / "gen").mkdir()
    (project / "src" / "gen" / "made.py").write_text("needle = 5\n", encoding="utf-8")

    trusted = run_tool(GrepTool(), {"pattern": "needle"}, _context(project, trusted_root=project))
    untrusted = run_tool(GrepTool(), {"pattern": "needle"}, _context(project, trusted_root=None))

    assert "gen/made.py" in trusted.text
    assert trusted.text == untrusted.text


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (FindTool(), {"path": "..", "pattern": "../tests/*.py"}),
        (GrepTool(), {"pattern": "needle", "path": "..", "glob": "../tests/*.py"}),
    ],
    ids=["find", "grep"],
)
def test_sibling_paths_shown_relative(project: Path, tool: object, arguments: dict) -> None:
    """Files above the working folder show as ../ paths, which globs can match."""

    result = run_tool(tool, arguments, _context(project, trusted_root=project))

    assert "../tests/test_app.py" in result.text
    assert str(project) not in result.text
