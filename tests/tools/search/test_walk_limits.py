"""grep and find stay within the stack and descriptors on deep or unusual trees."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from tests.tools.support import run_tool
from wisp.tools.base import Tool
from wisp.tools.builtin import FindTool, GrepTool
from wisp.tools.context import ToolContext
from wisp.tools.search import tools as search_module

_SEARCHES = [
    pytest.param(GrepTool(), {"pattern": "needle"}, "{path}:1:needle", id="grep"),
    pytest.param(FindTool(), {}, "{path}", id="find"),
]


@pytest.mark.parametrize(("tool", "arguments", "record"), _SEARCHES)
def test_skips_directories_too_deep(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    tool: Tool,
    arguments: dict[str, object],
    record: str,
) -> None:
    for relative in ("a/one.txt", "a/b/two.txt", "a/b/c/three.txt"):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(search_module, "_MAX_WALK_DEPTH", 2)

    result = run_tool(tool, arguments, ToolContext(cwd=tmp_path))

    records = [record.format(path=path) for path in ("a/b/two.txt", "a/one.txt")]
    assert result.text.splitlines() == [
        *records,
        "[truncated] Skipped 1 directory more than 2 levels deep. "
        "Search inside one to see its files.",
    ]
    assert result.truncated


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions that apply"
)
@pytest.mark.parametrize(("tool", "arguments", "record"), _SEARCHES)
def test_skips_directory_it_cannot_search(
    tmp_path: Path, tool: Tool, arguments: dict[str, object], record: str
) -> None:
    for relative in ("open/a.txt", "list-only/b.txt"):
        (tmp_path / relative).parent.mkdir(exist_ok=True)
        (tmp_path / relative).write_text("needle\n", encoding="utf-8")
    # Readable but not searchable: it lists, but its entries cannot be inspected.
    (tmp_path / "list-only").chmod(0o444)
    try:
        result = run_tool(tool, arguments, ToolContext(cwd=tmp_path))
    finally:
        (tmp_path / "list-only").chmod(0o755)

    assert result.text == record.format(path="open/a.txt")


# Runs a search with fewer descriptors than the tree is deep, below the depth limit.
_OUT_OF_DESCRIPTORS = """
import os, resource, sys
from pathlib import Path

import anyio

from wisp.tools.builtin import FindTool
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError

root = Path(sys.argv[1])
descriptor = os.open(root, os.O_RDONLY)
for _ in range(80):
    os.mkdir("d", dir_fd=descriptor)
    child = os.open("d", os.O_RDONLY, dir_fd=descriptor)
    os.close(descriptor)
    descriptor = child
os.close(os.open("bottom.txt", os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=descriptor))
os.close(descriptor)

resource.setrlimit(resource.RLIMIT_NOFILE, (48, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
try:
    result = anyio.run(FindTool().run, {}, ToolContext(cwd=root))
except ToolError as exc:
    print(f"error: {exc}")
else:
    print(f"result: {result.text}")
"""


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor limits")
def test_fails_when_out_of_descriptors(tmp_path: Path) -> None:
    script = tmp_path / "search.py"
    script.write_text(_OUT_OF_DESCRIPTORS, encoding="utf-8")
    tree = tmp_path / "tree"
    tree.mkdir()

    completed = subprocess.run(
        [sys.executable, str(script), str(tree)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )

    # Before, the native walk returned no files here and the Python walk failed.
    assert completed.stdout.startswith("error: Could not "), completed.stdout
    assert "Too many open files" in completed.stdout


def test_early_stop_still_reports_depth(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    for relative in ("a/b/c/deep.txt", "x1.txt", "x2.txt"):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(search_module, "_MAX_WALK_DEPTH", 2)

    result = run_tool(
        GrepTool(), {"pattern": "needle", "max_results": 1}, ToolContext(cwd=tmp_path)
    )

    # The match limit stops the walk early; the skipped directory still decides the marker.
    assert result.text.splitlines()[-1].startswith("[truncated] Skipped 1 directory")
