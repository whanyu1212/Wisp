from __future__ import annotations

import os
import threading
from collections.abc import Iterable
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from tests.tools.support import run_tool
from wisp.tools.builtin import FindTool
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolResult
from wisp.tools.search import tools as search_tools_module

_LIMIT_HINT = (
    "[truncated] Stopped at max_results=1. Narrow the search, or raise max_results to see more."
)


def test_find_tool_python_fallback_skips_symlinked_files_outside_cwd(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("", encoding="utf-8")
    link = workspace / "outside.py"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    context = ToolContext(cwd=workspace)

    result = run_tool(FindTool(), {"path": ".", "pattern": "*.py"}, context)

    assert result.text == "No files found"
    assert result.data == {"count": 0, "files": []}


def test_find_tool_python_fallback_filters_glob(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "one.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "two.txt").write_text("", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(FindTool(), {"path": ".", "pattern": "*.py"}, context)

    assert result.text == "pkg/one.py"
    assert result.data["files"] == ["pkg/one.py"]


def test_find_tool_prepares_glob_once_and_reuses_display_paths(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    paths = [tmp_path / name for name in ("alpha.py", "beta.txt", "gamma.md")]
    for path in paths:
        path.write_text("", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)
    expanded: list[str] = []
    displayed: list[Path] = []
    original_expand = search_tools_module._expand_brace_alternatives
    original_display = search_tools_module._display_walked_path

    def tracking_expand(pattern: str) -> tuple[str, ...]:
        expanded.append(pattern)
        return original_expand(pattern)

    def tracking_display(candidate: Path, resolved_cwd: Path) -> str:
        displayed.append(candidate)
        return original_display(candidate, resolved_cwd)

    monkeypatch.setattr(search_tools_module, "_expand_brace_alternatives", tracking_expand)
    monkeypatch.setattr(search_tools_module, "_display_walked_path", tracking_display)

    result = run_tool(
        FindTool(),
        {"path": ".", "pattern": "*.{py,txt}"},
        context,
    )

    assert result.text == "alpha.py\nbeta.txt"
    assert result.data["files"] == ["alpha.py", "beta.txt"]
    assert expanded == ["*.{py,txt}"]
    assert displayed == paths


def test_find_tool_python_fallback_skips_hidden_entries(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / ".hidden.py").write_text("", encoding="utf-8")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "secret.py").write_text("", encoding="utf-8")
    (tmp_path / "visible.py").write_text("", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(FindTool(), {"path": ".", "pattern": "*.py"}, context)

    assert result.text == "visible.py"
    assert result.data["files"] == ["visible.py"]


def test_find_handles_option_like_paths(tmp_path: Path) -> None:
    dash_dir = tmp_path / "-dash"
    dash_dir.mkdir()
    (dash_dir / "tool.py").write_text("", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(FindTool(), {"path": "-dash", "pattern": "*.py"}, context)

    assert result.text == "-dash/tool.py"


def test_find_returns_no_files_for_empty_directory(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    context = ToolContext(cwd=tmp_path)

    result = run_tool(FindTool(), {"path": "empty", "pattern": "*.py"}, context)

    assert result.text == "No files found"
    assert result.data == {"count": 0, "files": []}


def test_find_tool_skips_symlinked_files_when_opted_out(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("", encoding="utf-8")
    link = workspace / "outside.py"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    context = ToolContext(cwd=workspace, allow_outside_cwd=True)

    result = run_tool(FindTool(), {"path": ".", "pattern": "*.py"}, context)

    assert result.text == "No files found"
    assert result.data == {"count": 0, "files": []}


def test_find_tool_python_fallback_bounds_retained_matches(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    visited: list[str] = []

    def files(
        _path: Path,
        _context: ToolContext,
        **_kwargs: object,
    ) -> Iterable[Path]:
        for name in ("skip.txt", "c.py", "secret.key", "b.py", "a.py"):
            visited.append(name)
            yield tmp_path / name

    monkeypatch.setattr(search_tools_module, "_iter_files", files)
    context = ToolContext(cwd=tmp_path, protected_paths=("*.key",))

    result = run_tool(
        FindTool(),
        {"path": ".", "pattern": "*.py", "max_results": 1},
        context,
    )

    assert result.text == "a.py\n" + _LIMIT_HINT
    assert result.data == {"count": 2, "files": ["a.py"]}
    assert result.truncated is True
    assert visited == ["skip.txt", "c.py", "secret.key", "b.py", "a.py"]


def test_find_tool_python_fallback_preserves_global_sorted_prefix(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "first.py").write_text("", encoding="utf-8")
    (tmp_path / "y.py").write_text("", encoding="utf-8")
    (tmp_path / "z.py").write_text("", encoding="utf-8")

    result = run_tool(
        FindTool(),
        {"path": ".", "pattern": "*.py", "max_results": 1},
        ToolContext(cwd=tmp_path),
    )

    assert result.text == "a/first.py\n" + _LIMIT_HINT
    assert result.data == {"count": 2, "files": ["a/first.py"]}


def test_find_sorts_across_directories(tmp_path: Path) -> None:
    # The walk yields a/b.py first, but "a-c.py" sorts before it.
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b.py").write_text("", encoding="utf-8")
    (tmp_path / "a-c.py").write_text("", encoding="utf-8")

    result = run_tool(
        FindTool(),
        {"path": ".", "pattern": "*.py", "max_results": 1},
        ToolContext(cwd=tmp_path),
    )

    assert result.data == {"count": 2, "files": ["a-c.py"]}


@pytest.mark.skipif(os.name == "nt", reason="requires symlink creation")
def test_find_under_symlinked_cwd(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text("", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(project, target_is_directory=True)

    result = run_tool(FindTool(), {"path": "src", "pattern": "*.py"}, ToolContext(cwd=link))

    assert result.data == {"count": 1, "files": ["src/app.py"]}


def test_find_tool_python_fallback_skips_file_symlinks(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.py").write_text("", encoding="utf-8")
    (sub / "c.py").write_text("", encoding="utf-8")
    try:
        (sub / "z.py").symlink_to(tmp_path / "a.py")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    result = run_tool(
        FindTool(),
        {"path": "sub", "pattern": "*.py", "max_results": 1},
        ToolContext(cwd=tmp_path),
    )

    assert result.text == "sub/b.py\n" + _LIMIT_HINT
    assert result.data == {"count": 2, "files": ["sub/b.py"]}


def test_find_tool_python_fallback_runs_off_event_loop(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    started = threading.Event()
    release = threading.Event()
    original = search_tools_module._python_find

    def blocking_find(**kwargs: object) -> ToolResult:
        started.set()
        release.wait(timeout=2)
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(search_tools_module, "_python_find", blocking_find)

    async def scenario() -> None:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(FindTool().run, {}, ToolContext(cwd=tmp_path))
            assert await anyio.to_thread.run_sync(started.wait, 1)
            with anyio.fail_after(0.5):
                await anyio.sleep(0)
            release.set()

    anyio.run(scenario)


def test_find_tool_python_fallback_abandons_worker_wait_on_cancel(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    started = threading.Event()
    release = threading.Event()

    def blocking_find(**_kwargs: object) -> ToolResult:
        started.set()
        release.wait(timeout=2)
        return ToolResult(text="No files found")

    monkeypatch.setattr(search_tools_module, "_python_find", blocking_find)

    async def scenario() -> None:
        with anyio.fail_after(0.5):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(FindTool().run, {}, ToolContext(cwd=tmp_path))
                assert await anyio.to_thread.run_sync(started.wait, 1)
                task_group.cancel_scope.cancel()
        release.set()

    anyio.run(scenario)
