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

from tests.tui_e2e.support import rendered_without_spacing, welcome_ready


@pytest.mark.process
def test_rust_model_selection_survives_restart(tmp_path: Path) -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)
    # conftest gives both execve children the same isolated home and project.
    settings_path = Path.home() / ".wisp" / "settings.json"

    def run_selection(*, restart: bool, picker_only: bool = False) -> None:
        environment = {
            **os.environ,
            "WISP_RUST_TUI_BINARY": str(binary),
            "WISP_TRUST": "1",
        }
        for key in ("WISP_PROVIDER", "WISP_MODEL", "WISP_EFFORT"):
            environment.pop(key, None)
        if not restart:
            environment["WISP_PROVIDER"] = "fake"
        child_pid, terminal_fd = pty.fork()
        if child_pid == 0:
            os.execve(
                sys.executable,
                [
                    sys.executable,
                    "-m",
                    "wisp",
                    "tui",
                    "--session-dir",
                    str(tmp_path / ("restart" if restart else "initial")),
                ],
                environment,
            )
        fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 120, 0, 0))
        initial_terminal = termios.tcgetattr(terminal_fd)
        output = bytearray()
        status: int | None = None
        submitted = restart
        picker_phase = "startup" if picker_only else "done"
        picker_width = 121
        next_picker_frame = 0.0
        quit_sent = False
        quit_frame_requested = False
        context_redrawn = False
        selection_redrawn = False
        restored_label_redrawn = False
        deadline = time.monotonic() + 25
        try:
            while time.monotonic() < deadline:
                readable, _, _ = select.select([terminal_fd], [], [], 0.05)
                if readable:
                    try:
                        output.extend(os.read(terminal_fd, 65536))
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                if not context_redrawn and b"ctx" in output and b"~" in output:
                    # Context readiness replaces a loading hint. Request one full frame;
                    # differential writes can omit letters shared by the two hints.
                    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 121, 0, 0))
                    context_redrawn = True
                # Wait for welcome copy after hydration; the composer appears earlier.
                ready = context_redrawn and welcome_ready(output)
                if picker_phase == "startup" and ready and b"fake/fake" in output:
                    os.write(terminal_fd, b"/model\r")
                    picker_phase = "loading"
                    output.clear()
                elif picker_phase == "loading" and b"Current:" in output:
                    # The fake provider is hidden; inspect the first real catalog model
                    # before staging effort. Configuration makes no provider request.
                    picker_phase = "effort"
                    output.clear()
                elif (
                    picker_phase == "effort"
                    and b"Effort:" in output
                    and b"Default" in output
                    and b"apply" in output
                ):
                    os.write(terminal_fd, b"\x1b[C")
                    # Do not race navigation with a resize. If an intervening redraw
                    # rejects the key, the next unchanged frame retries it.
                    next_picker_frame = time.monotonic() + 0.1
                    output.clear()
                elif (
                    picker_phase == "effort"
                    and b"Effort:" in output
                    and b"Default" not in output
                    and b"apply" in output
                ):
                    os.write(terminal_fd, b"\r")
                    picker_phase = "applying"
                    submitted = True
                    output.clear()
                elif picker_phase == "applying" and b"Model selection applied." in output:
                    saved = json.loads(settings_path.read_text())
                    assert saved["provider"] != "fake" and saved["model"]
                    assert saved["effort"]
                    picker_phase = "done"
                elif not submitted and not picker_only and ready and b"fake/fake" in output:
                    os.write(terminal_fd, b"/model fake::custom-model effort\r")
                    submitted = True
                    output.clear()
                if (
                    not restart
                    and not picker_only
                    and not selection_redrawn
                    and b"Model selection applied." in output
                ):
                    # The footer update can reuse letters from the previous effort.
                    # Request a full frame so the raw PTY stream contains the label.
                    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 122, 0, 0))
                    selection_redrawn = True
                if (
                    restart
                    and ready
                    and not restored_label_redrawn
                    and b"fake/custom" in rendered_without_spacing(output)
                ):
                    # The restored footer label can arrive in a differential frame that
                    # skips letters shared with the previous frame. Request a full frame
                    # so the raw PTY stream contains the whole label.
                    output.clear()
                    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 122, 0, 0))
                    restored_label_redrawn = True
                applied = (
                    ready and b"fake/custom-model" in output and b"effort" in output
                    if restart
                    else (
                        b"Model selection applied." in output
                        and (picker_only or (b"custom-model" in output and b"effort" in output))
                    )
                )
                if submitted and applied and not quit_frame_requested:
                    # Observe a fresh ready frame after applying the model before
                    # sending Ctrl-C. Do not send it alongside the resize above.
                    output.clear()
                    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 124, 0, 0))
                    quit_frame_requested = True
                elif (
                    quit_frame_requested
                    and welcome_ready(output)
                    and (picker_only or (b"custom-model" in output and b"effort" in output))
                    and not quit_sent
                ):
                    os.write(terminal_fd, b"\x03")
                    quit_sent = True
                if picker_phase == "effort" and time.monotonic() >= next_picker_frame:
                    # Observe the staged effort in a full frame before activating it.
                    picker_width = 123 if picker_width == 122 else 122
                    fcntl.ioctl(
                        terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, picker_width, 0, 0)
                    )
                    next_picker_frame = time.monotonic() + 0.1
                    output.clear()
                waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
                if waited_pid == child_pid:
                    status = waited_status
                    break
            assert status is not None, (
                f"Rust model selection timed out in {picker_phase!r}: {bytes(output)!r}"
            )
            assert submitted and quit_sent, bytes(output)
            if not picker_only:
                assert b"custom-model" in output and b"effort" in output, bytes(output)
            assert os.waitstatus_to_exitcode(status) == 0, bytes(output)
            assert termios.tcgetattr(terminal_fd) == initial_terminal
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

    run_selection(restart=False, picker_only=True)
    run_selection(restart=False)
    assert json.loads(settings_path.read_text()) == {
        "provider": "fake",
        "model": "custom-model",
        "effort": "effort",
    }
    # No provider/model/effort environment overrides: prove defaults are restored.
    run_selection(restart=True)
