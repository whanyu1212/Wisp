"""Long bash output is shown as its start and its end (`_format_process_output_preview`)."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from tests.tools.support import run_tool
from wisp.tools.builtin import BashTool
from wisp.tools.context import ToolContext
from wisp.tools.shell.process import (
    PREVIEW_HEAD_BYTES,
    PREVIEW_TAIL_BYTES,
    STREAM_HEAD_BYTES,
    ProcessResult,
    _format_process_output_preview,
    _StreamTail,
    decode_stream_head,
)
from wisp.tools.truncation import TruncatedText, truncate_text

_PREVIEW_LIMIT = PREVIEW_HEAD_BYTES + PREVIEW_TAIL_BYTES + 200


def _preview(result: ProcessResult, notice: str | None = None) -> TruncatedText:
    return _format_process_output_preview(result, max_bytes=50_000, max_lines=2_000, notice=notice)


def _build_log(lines: int, *, error_at: int = 2) -> str:
    return "".join(
        "error: missing output path\n" if number == error_at else f"compiling module {number}\n"
        for number in range(1, lines + 1)
    )


def test_early_error_and_summary_shown() -> None:
    log = _build_log(3000) + "build failed\n"

    output = _preview(ProcessResult(exit_code=1, stdout=log, stderr=""))

    assert output.truncated is True
    assert output.text.startswith("Command exited with code 1: compiling module 1\n")
    assert "error: missing output path" in output.text
    assert output.text.endswith("compiling module 3000\nbuild failed")
    assert "omitted after the first 64 lines]" in output.text
    assert len(output.text.encode()) < _PREVIEW_LIMIT


def test_output_within_preview_shown_whole() -> None:
    log = _build_log(200)

    output = _preview(ProcessResult(exit_code=0, stdout=log, stderr=""))

    assert output.truncated is False
    assert output.text == f"Command exited with code 0: {log.rstrip()}"


def test_progress_redraws_survive_result_bounding() -> None:
    # Every tool result is bounded again, counting a bare carriage return as a line.
    log = "\r." * 3000 + "\nerror: final\n"
    notice = "[Full output saved: stdout output_id=abc. Read it with read_output.]"

    output = _preview(ProcessResult(exit_code=1, stdout=log, stderr=""), notice)
    bounded = truncate_text(output.text, max_bytes=50_000, max_lines=2_000)

    assert bounded.truncated is False
    assert bounded.text.endswith(f"error: final\n{notice}")


def test_large_stderr_without_stdout() -> None:
    log = _build_log(2500, error_at=3)

    output = _preview(ProcessResult(exit_code=2, stdout="", stderr=log))

    assert "error: missing output path" in output.text
    assert output.text.endswith("compiling module 2500")


def test_dropped_middle_uses_captured_head() -> None:
    # Capture kept stdout's first bytes and its last bytes; 90 KB between were dropped.
    result = ProcessResult(
        exit_code=1,
        stdout="tail 1\ntail 2\n",
        stderr="Traceback: boom\n",
        stdout_truncated=True,
        stdout_dropped_bytes=90_000,
        stdout_head="head 1\nerror: first\n",
        stdout_retained_bytes=14,
    )

    output = _preview(result)

    assert output.text == (
        "Command exited with code 1: head 1\nerror: first\n"
        "[truncated: 89,981 bytes omitted after the first 2 lines of stdout]\n"
        "tail 1\ntail 2\nTraceback: boom"
    )


def test_head_stays_in_first_stream() -> None:
    # The marker's line count must be an offset into one saved stream.
    stderr_tail = "".join(f"warning {n}\n" for n in range(5000, 10000))
    result = ProcessResult(
        exit_code=1,
        stdout="configured\n",
        stderr=stderr_tail,
        stderr_truncated=True,
        stderr_dropped_bytes=50_000,
        stderr_head="warning 1\n",
    )

    output = _preview(result)

    head, _, rest = output.text.partition("\n[truncated: ")
    assert head == "Command exited with code 1: configured"
    assert "omitted after the first 1 lines of stdout]\n" in rest
    assert "configured" not in rest and rest.endswith("warning 9999")


def test_both_streams_dropped() -> None:
    result = ProcessResult(
        exit_code=1,
        stdout="out tail\n",
        stderr="err tail\n",
        stdout_truncated=True,
        stderr_truncated=True,
        stdout_dropped_bytes=60_000,
        stderr_dropped_bytes=60_000,
        stdout_head="out head\n",
        stderr_head="err head\n",
    )

    output = _preview(result)

    assert output.text.startswith("Command exited with code 1: out head\n[truncated: 1")
    assert "first 1 lines of stdout]" in output.text
    assert output.text.endswith("out tail\nerr tail") and "err head" not in output.text


@pytest.mark.parametrize(("max_bytes", "max_lines"), [(1_000, 8), (1_000, 40), (2_500, 8)])
def test_preview_within_small_budget(max_bytes: int, max_lines: int) -> None:
    notice = "[Full output saved: stdout output_id=abc. Read it with read_output.]"
    result = ProcessResult(exit_code=1, stdout=_build_log(3000), stderr="")

    output = _format_process_output_preview(
        result, max_bytes=max_bytes + len(notice) + 1, max_lines=max_lines + 1, notice=notice
    )

    body = output.text.removeprefix("Command exited with code 1: ")
    assert "[truncated: " in body and body.endswith(notice)
    assert len(body.encode()) <= max_bytes + len(notice) + 1
    assert len(body.split("\n")) <= max_lines + 1


def test_notice_kept_after_preview() -> None:
    notice = "[Full output saved: stdout output_id=abc (90 KB). Read it with read_output.]"

    output = _preview(ProcessResult(exit_code=1, stdout=_build_log(3000), stderr=""), notice)

    assert output.text.endswith(f"compiling module 3000\n{notice}")


def test_small_budget_keeps_tail() -> None:
    output = _format_process_output_preview(
        ProcessResult(exit_code=0, stdout=_build_log(100), stderr=""), max_bytes=300, max_lines=50
    )

    assert output.text.startswith("Command exited with code 0: [truncated] ")
    assert output.text.endswith("compiling module 100")
    assert len(output.text.encode()) <= 300 + len("Command exited with code 0: ")


def test_head_captured_only_when_middle_dropped() -> None:
    whole, cut = _StreamTail(100), _StreamTail(100)
    whole.append(b"x" * 100)
    # The reader appends 8 KB chunks; the head must fill across them.
    data = b"a" * STREAM_HEAD_BYTES + "é".encode() + b"b" * 5000
    for offset in range(0, len(data), 8192):
        cut.append(data[offset : offset + 8192])

    assert whole.result().head == b""
    head = cut.result().head
    assert len(head) == STREAM_HEAD_BYTES and decode_stream_head(head) == "a" * STREAM_HEAD_BYTES


def test_head_drops_cut_character() -> None:
    assert decode_stream_head("ab語".encode()[:4]) == "ab"


@pytest.mark.process
def test_bash_shows_both_ends(tmp_path: Path) -> None:
    code = (
        "print('error: first failure')\n"
        "for n in range(5000):\n"
        "    print(f'progress line {n:05d} ' + 'x' * 20)\n"
        "print('build failed')"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"

    result = run_tool(BashTool(), {"command": command}, ToolContext(cwd=tmp_path))

    assert result.truncated is True
    assert "error: first failure" in result.text and "build failed" in result.text
    assert "progress line 02500" not in result.text
    assert len(result.text.encode()) < _PREVIEW_LIMIT + 300
    assert result.text.endswith("Read it with read_output.]")
    assert result.data["stdout_output_id"] in result.text


@pytest.mark.process
def test_bash_saves_output_capture_kept(tmp_path: Path) -> None:
    # About 20 KB: capture keeps all of it, but the preview leaves out the middle.
    code = "for n in range(800):\n    print(f'line {n:04d} ' + 'y' * 15)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"

    result = run_tool(BashTool(), {"command": command}, ToolContext(cwd=tmp_path))

    assert result.data["stdout_truncated"] is False and result.truncated is True
    assert "line 0400" not in result.text
    assert result.data["stdout_output_id"] in result.text
