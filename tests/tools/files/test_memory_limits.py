"""read and edit keep memory bounded on huge files and lines."""

from __future__ import annotations

import tracemalloc
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from tests.tools.support import run_tool
from wisp.tools.builtin import EditTool, ReadTool
from wisp.tools.context import ToolContext
from wisp.tools.files import operations as operations_module
from wisp.tools.result import ToolError


@pytest.mark.parametrize(
    ("content", "line"),
    [
        pytest.param("ab\r\ncd\r\n", 2, id="crlf-at-limit"),
        pytest.param("ab\rcd\n", 2, id="cr-at-limit"),
        pytest.param("abcdefgh\ncd\n", 2, id="long-line"),
        pytest.param("ab\r\r\ncd\n", 3, id="cr-then-crlf"),
    ],
)
def test_line_ends_at_read_limit(tmp_path: Path, content: str, line: int) -> None:
    path = tmp_path / "lines.txt"
    path.write_bytes(content.encode())

    # A 2-byte budget reads each line at most 3 characters at a time, so a CRLF can
    # straddle the limit; reading it as two line ends would shift every later line.
    result = operations_module._read_line_slice(
        path, offset=line, limit=1, max_bytes=2, max_lines=10
    )

    assert (result.text, result.partial_line) == ("cd", line)


@pytest.mark.parametrize("offset", [1, 2], ids=["clipped", "skipped"])
def test_read_holds_long_line_in_budget(tmp_path: Path, offset: int) -> None:
    (tmp_path / "one-line.txt").write_text("x" * 4_000_000 + "\ntail\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    tracemalloc.start()
    try:
        result = run_tool(ReadTool(), {"path": "one-line.txt", "offset": offset}, context)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.text.startswith("x" if offset == 1 else "tail")
    assert peak < 1_000_000


def test_edit_refuses_large_file(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    target = tmp_path / "big.txt"
    target.write_text("0123456789\n", encoding="utf-8")
    monkeypatch.setattr(operations_module, "_MAX_EDIT_BYTES", 10)

    with pytest.raises(ToolError, match="big.txt is 11 bytes, more than the 10 bytes edit"):
        run_tool(
            EditTool(),
            {"path": "big.txt", "edits": [{"oldText": "0", "newText": "1"}]},
            ToolContext(cwd=tmp_path),
        )

    assert target.read_text(encoding="utf-8") == "0123456789\n"
