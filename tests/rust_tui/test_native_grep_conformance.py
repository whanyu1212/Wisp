"""Conformance tests for the optional native literal grep scanner."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import anyio
import pytest

from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.search import tools as search_tools_module
from wisp.tools.search.tools import GrepTool

native = pytest.importorskip("wisp._native")
if not hasattr(native, "scan_literal_fd"):
    pytest.skip("installed native extension does not include grep", allow_module_level=True)


def _run_grep(
    root: Path,
    arguments: dict[str, object],
    *,
    backend: Literal["python", "native"],
    max_output_bytes: int = 50_000,
    max_output_lines: int = 2_000,
) -> ToolResult:
    async def run() -> ToolResult:
        tool = GrepTool(_scanner_backend=backend)
        try:
            return await tool.run(
                arguments,
                ToolContext(
                    cwd=root,
                    max_output_bytes=max_output_bytes,
                    max_output_lines=max_output_lines,
                ),
            )
        finally:
            await tool.aclose()

    return anyio.run(run)


def _assert_backends_agree(
    root: Path,
    arguments: dict[str, object],
    *,
    max_output_bytes: int = 50_000,
    max_output_lines: int = 2_000,
) -> ToolResult:
    python_result = _run_grep(
        root,
        arguments,
        backend="python",
        max_output_bytes=max_output_bytes,
        max_output_lines=max_output_lines,
    )
    native_result = _run_grep(
        root,
        arguments,
        backend="native",
        max_output_bytes=max_output_bytes,
        max_output_lines=max_output_lines,
    )
    assert native_result == python_result
    return native_result


def test_native_literal_grep_matches_splitlines_and_context(tmp_path: Path) -> None:
    boundaries = (
        "\n",
        "\r",
        "\r\n",
        "\v",
        "\f",
        "\x1c",
        "\x1d",
        "\x1e",
        "\x85",
        "\u2028",
        "\u2029",
    )
    text = "".join(
        f"before {index}{boundary}needle {index}{boundary}"
        for index, boundary in enumerate(boundaries)
    )
    (tmp_path / "mixed.txt").write_text(text + "tail", encoding="utf-8")

    result = _assert_backends_agree(
        tmp_path,
        {"pattern": "needle", "literal": True, "context": 1},
    )

    assert result.data["count"] == len(boundaries)


def test_native_literal_grep_discards_binary_and_invalid_utf8_files(tmp_path: Path) -> None:
    (tmp_path / "binary.txt").write_bytes(b"needle\n" + (b"x" * 70_000) + b"\0")
    (tmp_path / "invalid.txt").write_bytes(b"needle\n" + (b"x" * 70_000) + b"\xff")
    (tmp_path / "text.txt").write_text("needle\n", encoding="utf-8")

    result = _assert_backends_agree(
        tmp_path,
        {"pattern": "needle", "literal": True},
    )

    assert result.data["matches"] == ["text.txt:1:needle"]


def test_native_literal_grep_matches_unicode_and_exact_limit(tmp_path: Path) -> None:
    (tmp_path / "unicode.txt").write_text("π needle\nπ needle\n", encoding="utf-8")

    result = _assert_backends_agree(
        tmp_path,
        {"pattern": "π needle", "literal": True, "max_results": 1},
    )

    assert result.data["count"] == 1
    assert result.truncated is True


def test_native_literal_grep_matches_output_bounds(tmp_path: Path) -> None:
    (tmp_path / "bounded.txt").write_text(
        "before\nneedle with a long matching record\nafter\n",
        encoding="utf-8",
    )

    result = _assert_backends_agree(
        tmp_path,
        {"pattern": "needle", "literal": True, "context": 1},
        max_output_bytes=24,
        max_output_lines=2,
    )

    assert result.data["count"] == 1
    assert result.truncated is True


@pytest.mark.parametrize("terminator", ["", "\n"])
def test_native_literal_grep_matches_long_line_error(tmp_path: Path, terminator: str) -> None:
    (tmp_path / "minified.txt").write_text("x" * 1_000_001 + terminator, encoding="utf-8")
    arguments = {"pattern": "needle", "literal": True}

    messages: list[str] = []
    for backend in ("python", "native"):
        with pytest.raises(ToolError) as caught:
            _run_grep(tmp_path, arguments, backend=backend)
        messages.append(str(caught.value))

    assert messages == [
        "grep encountered a line longer than 1000000 characters",
        "grep encountered a line longer than 1000000 characters",
    ]


def test_native_literal_grep_falls_back_for_surrogateescaped_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "data.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(
        search_tools_module,
        "_display_walked_path",
        lambda _path, _root: "file-\udcff.txt",
    )

    result = _run_grep(
        tmp_path,
        {"pattern": "absent", "literal": True},
        backend="native",
    )

    assert result.data == {"count": 0, "matches": []}


def test_native_literal_grep_falls_back_for_unrepresentable_limits(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("needle\n", encoding="utf-8")

    result = _assert_backends_agree(
        tmp_path,
        {"pattern": "needle", "literal": True, "max_results": 2**64},
    )

    assert result.data["count"] == 1


def test_native_literal_scanner_reads_the_open_descriptor_after_path_replacement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data.txt"
    path.write_text("original needle\n", encoding="utf-8")
    descriptor = os.open(path, os.O_RDONLY)
    try:
        path.rename(tmp_path / "moved.txt")
        path.write_text("replacement\n", encoding="utf-8")
        result = native.scan_literal_fd(
            descriptor,
            "needle",
            "data.txt",
            context_lines=0,
            remaining_matches=10,
            prior_lines=0,
            prior_bytes=0,
            prefix_separator=False,
            max_output_lines=100,
            max_output_bytes=10_000,
            max_line_chars=1_000_000,
            cancellation=native.GrepCancellation(),
        )
    finally:
        os.close(descriptor)

    assert result.status == "complete"
    assert result.lines == ["data.txt:1:original needle"]


def test_native_literal_scanner_honors_cancellation_before_read(tmp_path: Path) -> None:
    path = tmp_path / "data.txt"
    path.write_text("needle\n", encoding="utf-8")
    cancellation = native.GrepCancellation()
    cancellation.cancel()
    descriptor = os.open(path, os.O_RDONLY)
    try:
        result = native.scan_literal_fd(
            descriptor,
            "needle",
            "data.txt",
            context_lines=0,
            remaining_matches=10,
            prior_lines=0,
            prior_bytes=0,
            prefix_separator=False,
            max_output_lines=100,
            max_output_bytes=10_000,
            max_line_chars=1_000_000,
            cancellation=cancellation,
        )
    finally:
        os.close(descriptor)

    assert result.status == "cancelled"
    assert result.lines == []
