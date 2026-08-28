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


@pytest.mark.process
@pytest.mark.parametrize(
    ("exercise_prompt", "expected_exit"),
    [(True, 0), (False, 1)],
    ids=["prompt-stream-and-quit", "frontend-killed"],
)
def test_rust_tui_cross_language_smoke(
    tmp_path: Path,
    exercise_prompt: bool,
    expected_exit: int,
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
        }
        os.execve(
            sys.executable,
            [
                sys.executable,
                "-m",
                "wisp",
                "tui",
                "--renderer",
                "rust",
                "--session-dir",
                str(tmp_path),
            ],
            environment,
        )

    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    initial_terminal = termios.tcgetattr(terminal_fd)
    output = bytearray()
    status: int | None = None
    deadline = time.monotonic() + 20
    prompt_sent = False
    response_seen = False
    quit_sent = False
    rust_process_group: int | None = None
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([terminal_fd], [], [], 0.05)
            if readable:
                try:
                    output.extend(os.read(terminal_fd, 65536))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            if not prompt_sent and b"Type a prompt below to start." in output:
                if not exercise_prompt:
                    rust_process_group = os.tcgetpgrp(terminal_fd)
                    os.kill(rust_process_group, signal.SIGKILL)
                    quit_sent = True
                else:
                    os.write(terminal_fd, b"hello rust\r")
                prompt_sent = True
            if exercise_prompt and not response_seen and b"fake response" in output:
                response_seen = True
            if (
                exercise_prompt
                and response_seen
                and not quit_sent
                and output.rfind(b"idle") > output.rfind(b"running")
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
