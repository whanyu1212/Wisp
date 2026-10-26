"""write and edit refuse special files, keep replacements private and make renames durable."""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from tests.tools.support import run_tool
from wisp.tools.base import Tool
from wisp.tools.builtin import EditTool, ReadTool, WriteTool
from wisp.tools.context import ToolContext
from wisp.tools.files import operations as operations_module
from wisp.tools.result import ToolError

_CHANGES = [
    pytest.param(WriteTool(), {"path": "a.txt", "content": "new\n"}, id="write"),
    pytest.param(
        EditTool(), {"path": "a.txt", "edits": [{"oldText": "old", "newText": "new"}]}, id="edit"
    ),
]


@pytest.mark.skipif(os.name == "nt", reason="FIFOs are POSIX")
@pytest.mark.parametrize("mode", [0o644, 0o222], ids=["read-write", "write-only"])
def test_refuses_fifo(tmp_path: Path, mode: int) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo, mode)
    outcome: list[BaseException | None] = []

    def write() -> None:
        try:
            run_tool(WriteTool(), {"path": "pipe", "content": "x"}, ToolContext(cwd=tmp_path))
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread
            outcome.append(exc)
        else:
            outcome.append(None)

    worker = threading.Thread(target=write, daemon=True)
    worker.start()
    worker.join(5)
    if worker.is_alive():
        # Release a write blocked waiting for a reader before failing.
        fifo.chmod(0o644)
        os.close(os.open(fifo, os.O_RDONLY | os.O_NONBLOCK))
        pytest.fail("write blocked on a FIFO")

    assert isinstance(outcome[0], ToolError)
    assert "Not a regular file: pipe" in str(outcome[0])
    assert stat.S_ISFIFO(fifo.lstat().st_mode)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.parametrize(("tool", "arguments"), _CHANGES)
def test_replacement_starts_private(
    tmp_path: Path, monkeypatch: MonkeyPatch, tool: Tool, arguments: dict[str, object]
) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o600)
    modes: list[int] = []
    original = operations_module._apply_replacement_metadata  # noqa: SLF001

    def record_mode(descriptor: int, metadata: object) -> None:
        # The new content is already in the temporary file at this point.
        modes.append(stat.S_IMODE(os.fstat(descriptor).st_mode))
        original(descriptor, metadata)  # type: ignore[arg-type]

    monkeypatch.setattr(operations_module, "_apply_replacement_metadata", record_mode)

    run_tool(tool, arguments, ToolContext(cwd=tmp_path))

    assert modes == [0o600]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory sync")
@pytest.mark.parametrize(("tool", "arguments"), _CHANGES)
def test_syncs_directory_after_rename(
    tmp_path: Path, monkeypatch: MonkeyPatch, tool: Tool, arguments: dict[str, object]
) -> None:
    (tmp_path / "a.txt").write_text("old\n", encoding="utf-8")
    synced: list[int] = []
    fsync = os.fsync

    def record_sync(descriptor: int) -> None:
        info = os.fstat(descriptor)
        if stat.S_ISDIR(info.st_mode):
            synced.append(info.st_ino)
        fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_sync)

    run_tool(tool, arguments, ToolContext(cwd=tmp_path))

    assert synced == [tmp_path.stat().st_ino]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        pytest.param(ReadTool(), {"path": "a\0b"}, id="read"),
        pytest.param(WriteTool(), {"path": "a\0b", "content": "x"}, id="write"),
    ],
)
def test_nul_path_is_argument_error(
    tmp_path: Path, tool: Tool, arguments: dict[str, object]
) -> None:
    with pytest.raises(ToolError, match="contains a NUL character") as caught:
        run_tool(tool, arguments, ToolContext(cwd=tmp_path))

    assert caught.value.failure_code == "invalid_arguments"
