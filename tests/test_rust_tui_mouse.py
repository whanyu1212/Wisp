"""Opt-in native mouse input through the real Python launcher and a pseudo-terminal."""

from __future__ import annotations

import errno
import fcntl
import json
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
@pytest.mark.parametrize("mouse_enabled", [False, True], ids=["keyboard", "mouse"])
def test_native_mouse_moves_only_the_opted_in_composer_cursor(
    tmp_path: Path, mouse_enabled: bool
) -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)
    session_dir = tmp_path / "sessions"
    child_pid, terminal_fd = pty.fork()
    if child_pid == 0:
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
                str(session_dir),
            ],
            {
                **os.environ,
                "WISP_PROVIDER": "fake",
                "WISP_MODEL": "",
                "WISP_TRUST": "1",
                "WISP_RUST_TUI_BINARY": str(binary),
                "WISP_TUI_MOUSE": "1" if mouse_enabled else "0",
            },
        )
    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    initial_terminal = termios.tcgetattr(terminal_fd)
    output = bytearray()
    all_output = bytearray()
    status: int | None = None
    phase = "startup"
    context_redrawn = False
    deadline = time.monotonic() + 25
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([terminal_fd], [], [], 0.05)
            if readable:
                try:
                    chunk = os.read(terminal_fd, 65536)
                    output.extend(chunk)
                    all_output.extend(chunk)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            if not context_redrawn and b"ctx" in output and b"~" in output:
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 81, 0, 0))
                context_redrawn = True
                output.clear()
                continue
            if (
                phase == "startup"
                and context_redrawn
                and b"Type a prompt below to start." in output
            ):
                os.write(terminal_fd, b"mouse draft")
                phase = "draft"
                output.clear()
            elif phase == "draft" and b"draft" in output:
                # SGR is one-based. At 24 rows the one-line composer starts at
                # column 2, row 22. Release is deliberately ignored by navigation.
                os.write(terminal_fd, b"\x1b[<0;2;22M\x1b[<0;2;22m")
                output.clear()
                if mouse_enabled:
                    phase = "cursor moved"
                else:
                    os.write(terminal_fd, b"X\r")
                    phase = "submitted"
            elif phase == "cursor moved" and b"\x1b[22;2H" in output:
                os.write(terminal_fd, b"X\r")
                phase = "submitted"
                output.clear()
            elif phase == "submitted" and b"response" in output:
                # A fast response can finish without ever painting "running", so
                # the already-idle header need not appear in differential output.
                # Request a full current frame before deciding it is safe to quit.
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 82, 0, 0))
                phase = "settled frame"
                output.clear()
            elif phase == "settled frame" and b"idle" in output:
                os.write(terminal_fd, b"\x03")
                phase = "quit"
            waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                status = waited_status
                break
        if status is None:
            pytest.fail(f"Mouse PTY timed out in {phase!r}: {bytes(output)!r}")
        assert phase == "quit", bytes(output)
        assert os.waitstatus_to_exitcode(status) == 0, bytes(output)
        assert termios.tcgetattr(terminal_fd) == initial_terminal
        for mode in (1000, 1006):
            assert (f"\x1b[?{mode}h".encode() in all_output) is mouse_enabled
            if mouse_enabled:
                assert f"\x1b[?{mode}l".encode() in all_output
        assert b"\x1b[?1003h" not in all_output
    finally:
        if status is None:
            try:
                os.killpg(os.tcgetpgrp(terminal_fd), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        os.close(terminal_fd)
        if status is None:
            # Closing the PTY releases blocked Darwin writes before reaping.
            cleanup_deadline = time.monotonic() + 5
            while time.monotonic() < cleanup_deadline:
                if os.waitpid(child_pid, os.WNOHANG)[0] == child_pid:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError(f"Could not reap mouse-test child {child_pid}")
    messages = [
        entry["message"]
        for path in session_dir.glob("*.jsonl")
        for line in path.read_bytes().split(b"\n")[:-1]
        if (entry := json.loads(line)).get("kind") == "message"
    ]
    prompts = [message["content"] for message in messages if message["role"] == "user"]
    assert prompts == ["Xmouse draft" if mouse_enabled else "mouse draftX"]
