from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import time
from pathlib import Path

import pytest

from tests.tui_e2e.support import (
    complete_logged_commands,
    run_settled,
    welcome_ready,
)


def _frame(body: bytes) -> bytes:
    return b"\x1b[?2026h" + body + b"\x1b[?2026l"


def test_run_settled_ignores_a_partially_read_frame() -> None:
    """A frame cut off after the response but before its status cells is not idle.

    This is the CI failure on main 194e3d3a: the test saw the response with the
    welcome frame's ``idle`` still last, sent Ctrl+C while the prompt was running,
    and the TUI cancelled instead of quitting.
    """

    welcome = _frame(b"Ask Wisp anything idle")
    running = _frame(b"fake response to: hi  working")
    partial = welcome + running[: running.index(b"working")]

    assert not run_settled(partial, len(welcome), b"fake", b"response", b"to:")
    assert not run_settled(welcome + running, len(welcome), b"fake", b"response", b"to:")
    settled = welcome + running + _frame(b"idle")
    assert run_settled(settled, len(welcome), b"fake", b"response", b"to:")


def test_run_settled_requires_fragments_after_offset() -> None:
    output = _frame(b"fake response to: old idle")

    assert not run_settled(output, len(output), b"response")
    assert run_settled(output, 0, b"response")


def test_command_log_ignores_incomplete_records(tmp_path: Path) -> None:
    path = tmp_path / "commands.jsonl"
    assert complete_logged_commands(path) == []
    path.write_bytes(b'{"type":"prompt"}\n{"type":"follow_up"')
    assert complete_logged_commands(path) == [{"type": "prompt"}]
    with path.open("ab") as log:
        log.write(b"}\n")
    assert complete_logged_commands(path) == [{"type": "prompt"}, {"type": "follow_up"}]


@pytest.mark.process
@pytest.mark.parametrize("mouse_enabled", [False, True], ids=["keyboard", "mouse"])
@pytest.mark.parametrize(
    ("exercise_prompt", "expected_exit"),
    [(True, 0), (False, 1)],
    ids=["prompt-stream-and-quit", "frontend-killed"],
)
def test_rust_tui_cross_language_smoke(
    tmp_path: Path,
    exercise_prompt: bool,
    expected_exit: int,
    mouse_enabled: bool,
) -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)

    child_pid, terminal_fd = pty.fork()
    if child_pid == 0:
        environment = {
            **os.environ,
            "WISP_PROVIDER": "fake",
            "WISP_MODEL": "",
            "WISP_RUST_TUI_BINARY": str(binary),
            "WISP_TRUST": "1",
            "WISP_TUI_MOUSE": "1" if mouse_enabled else "0",
        }
        os.execve(
            sys.executable,
            [
                sys.executable,
                "-m",
                "wisp",
                "tui",
                "--session-dir",
                str(tmp_path),
            ],
            environment,
        )

    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    initial_terminal = termios.tcgetattr(terminal_fd)
    output = bytearray()
    status: int | None = None
    context_redrawn = False
    deadline = time.monotonic() + 20
    prompt_sent = False
    response_seen = False
    quit_sent = False
    terminal_output = bytearray()
    rust_process_group: int | None = None
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([terminal_fd], [], [], 0.05)
            if readable:
                try:
                    chunk = os.read(terminal_fd, 65536)
                    output.extend(chunk)
                    terminal_output.extend(chunk)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            if not context_redrawn and b"ctx" in output and b"~" in output:
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 81, 0, 0))
                context_redrawn = True
                output.clear()
                continue
            if context_redrawn and not prompt_sent and welcome_ready(output):
                if not exercise_prompt:
                    rust_process_group = os.tcgetpgrp(terminal_fd)
                    os.kill(rust_process_group, signal.SIGKILL)
                    quit_sent = True
                else:
                    os.write(terminal_fd, b"hello rust\r")
                prompt_sent = True
            # Streaming can draw "fake" and "response" in separate frames with
            # cursor movements between them; the raw PTY bytes need not be adjacent.
            if exercise_prompt and not response_seen and b"response" in output:
                response_seen = True
            if (
                exercise_prompt
                and response_seen
                and not quit_sent
                and run_settled(output, 0, b"response")
            ):
                # Ctrl-C while the prompt is still running now cancels rather
                # than quitting. Wait until the header returns to idle first.
                os.write(terminal_fd, b"\x03")
                quit_sent = True
            waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                status = waited_status
                break
        if status is None:
            pytest.fail(f"Rust TUI smoke test timed out; output={bytes(output)!r}")
    finally:
        if status is None:
            try:
                foreground_pgrp = os.tcgetpgrp(terminal_fd)
                os.killpg(foreground_pgrp, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(child_pid, 0)

    assert prompt_sent, bytes(output)
    assert quit_sent, bytes(output)
    for mode in (1000, 1006):
        assert (f"\x1b[?{mode}h".encode() in terminal_output) is mouse_enabled
        if mouse_enabled:
            assert f"\x1b[?{mode}l".encode() in terminal_output
    assert b"\x1b[?1003h" not in terminal_output, "navigation must not request all-motion reports"
    if exercise_prompt:
        assert response_seen, bytes(output)
        # Ratatui may place cursor-control sequences between adjacent cells, so
        # assert the submitted prompt's words independently in the PTY stream.
        assert b"hello" in output
        assert b"rust" in output
    assert os.waitstatus_to_exitcode(status) == expected_exit, bytes(output)
    assert termios.tcgetattr(terminal_fd) == initial_terminal
    if rust_process_group is not None:
        with pytest.raises(ProcessLookupError):
            os.killpg(rust_process_group, 0)
