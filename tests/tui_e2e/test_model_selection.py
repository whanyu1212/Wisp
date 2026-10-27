from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import select
import struct
import sys
import termios
import time
from pathlib import Path

import pytest

from tests.tui_e2e.support import (
    complete_frames,
    reap,
    screen_rows,
    screen_shows,
    welcome_ready,
)


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
        phase = "startup"
        # A key sent while a frame is pending, or while the catalog is still
        # arriving, may be ignored by design. Resend only keys whose effect has not
        # appeared, and only where a repeat is harmless.
        resend_at = 0.0
        deadline = time.monotonic() + 25

        def shows(text: str) -> bool:
            return screen_shows(output, text)

        def send(value: bytes, *, retry_after: float = 0.5) -> None:
            nonlocal resend_at
            os.write(terminal_fd, value)
            resend_at = time.monotonic() + retry_after

        try:
            while time.monotonic() < deadline:
                readable, _, _ = select.select([terminal_fd], [], [], 0.05)
                if readable:
                    try:
                        output.extend(os.read(terminal_fd, 65536))
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
                if waited_pid == child_pid:
                    status = waited_status
                    break
                now = time.monotonic()
                # Wait for welcome copy after hydration; the composer appears earlier.
                ready = welcome_ready(output)
                if phase == "startup" and ready:
                    if restart:
                        phase = "restored"
                    elif shows("fake fake"):
                        command = (
                            b"/model\r" if picker_only else b"/model fake::custom-model effort\r"
                        )
                        send(command, retry_after=1)
                        phase = "picker" if picker_only else "applying"
                elif phase == "restored" and shows("custom-model fake · effort"):
                    phase = "quitting"
                elif phase == "picker":
                    if shows("Effort: Default  ←/→ change"):
                        # Effort is clamped, so a repeated Right cannot wrap back.
                        if now >= resend_at:
                            send(b"\x1b[C")
                    elif shows("←/→ change") and shows("Enter apply"):
                        send(b"\r")
                        phase = "applying"
                    elif shows("› /model") and now >= resend_at:
                        # The submit was ignored; the draft is still in the composer.
                        send(b"\r", retry_after=1)
                elif phase == "applying":
                    if picker_only and shows("Model selection applied."):
                        saved = json.loads(settings_path.read_text())
                        assert saved["provider"] != "fake" and saved["model"]
                        assert saved["effort"]
                        phase = "quitting"
                    elif not picker_only and shows("custom-model fake · effort"):
                        phase = "quitting"
                    elif now >= resend_at and (
                        (picker_only and shows("←/→ change") and shows("Enter apply"))
                        or (not picker_only and shows("› /model fake::custom-model effort"))
                    ):
                        # Applying replaces the picker hint and clears the composer.
                        send(b"\r", retry_after=1)
                if phase == "quitting" and shows("idle"):
                    os.write(terminal_fd, b"\x03")
                    phase = "exiting"
            assert status is not None, f"Rust model selection timed out in {phase!r}: " + "\n".join(
                screen_rows(complete_frames(output))
            )
            assert phase == "exiting", f"Rust TUI exited in {phase!r}"
            assert os.waitstatus_to_exitcode(status) == 0, bytes(output)
            assert termios.tcgetattr(terminal_fd) == initial_terminal
        finally:
            if status is None:
                reap(child_pid, terminal_fd)
            else:
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
