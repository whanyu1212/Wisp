"""Smoke-test the Rust TUI through an installed Wisp environment."""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import pty
import resource
import select
import signal
import struct
import sys
import termios
import time
from pathlib import Path


def run(wisp: Path, session_dir: Path) -> dict[str, float | int]:
    """Submit one fake-provider prompt through the installed Rust TUI.

    Args:
        wisp: Installed Wisp console script.
        session_dir: Disposable session directory.

    Returns:
        Ready-frame, total runtime, and child-process peak RSS measurements.

    Raises:
        RuntimeError: If startup, response, exit, or terminal restoration fails.
    """

    started = time.monotonic()
    ready_seconds: float | None = None
    child_pid, terminal_fd = pty.fork()
    if child_pid == 0:
        environment = {
            **os.environ,
            "WISP_PROVIDER": "fake",
            "WISP_MODEL": "",
            "WISP_TRUST": "1",
            "WISP_TUI_MOUSE": "0",
        }
        environment.pop("WISP_RUST_TUI_BINARY", None)
        os.execve(
            str(wisp),
            [
                str(wisp),
                "tui",
                "--renderer",
                "rust",
                "--session-dir",
                str(session_dir),
            ],
            environment,
        )

    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 81, 0, 0))
    initial_terminal = termios.tcgetattr(terminal_fd)
    output = bytearray()
    prompt_sent = False
    response_seen = False
    quit_sent = False
    status: int | None = None
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([terminal_fd], [], [], 0.05)
            if readable:
                try:
                    output.extend(os.read(terminal_fd, 65536))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            if not prompt_sent and b"Type a prompt or / for commands." in output:
                ready_seconds = time.monotonic() - started
                os.write(terminal_fd, b"installed wheel smoke\r")
                prompt_sent = True
            if prompt_sent and b"response" in output:
                response_seen = True
            if response_seen and not quit_sent and output.rfind(b"idle") > output.rfind(b"working"):
                os.write(terminal_fd, b"\x03")
                quit_sent = True
            waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                status = waited_status
                break
        if status is None:
            raise RuntimeError(f"installed Rust TUI smoke timed out: {bytes(output[-8000:])!r}")
        exit_code = os.waitstatus_to_exitcode(status)
        if exit_code != 0 or not response_seen:
            raise RuntimeError(
                f"installed Rust TUI smoke failed with {exit_code}: {bytes(output[-8000:])!r}"
            )
        if termios.tcgetattr(terminal_fd) != initial_terminal:
            raise RuntimeError("installed Rust TUI did not restore terminal attributes")
        if ready_seconds is None:
            raise RuntimeError("installed Rust TUI never reached a ready frame")
        rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        rss_bytes = int(rss if sys.platform == "darwin" else rss * 1024)
        return {
            "binary_bytes": (wisp.parent / "wisp-tui").stat().st_size,
            "ready_frame_seconds": ready_seconds,
            "total_seconds": time.monotonic() - started,
            "max_rss_bytes": rss_bytes,
        }
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
            os.waitpid(child_pid, 0)
        os.close(terminal_fd)


def main() -> None:
    """Run the installed-wheel smoke from the command line."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--wisp", type=Path, required=True)
    parser.add_argument("--session-dir", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(run(arguments.wisp, arguments.session_dir), sort_keys=True))


if __name__ == "__main__":
    main()
