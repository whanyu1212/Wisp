from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import re
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
    display_width,
    frame_cursor,
    run_settled,
    welcome_ready,
)


@pytest.mark.process
def test_rust_tui_skill_browser_expands_through_python(tmp_path: Path) -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)
    project = tmp_path / "project"
    skill = project / ".wisp" / "skills" / "0-review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        """---
name: 0-review
description: Inspect changes carefully
---
SKILL_EXPANSION_MARKER
""",
        encoding="utf-8",
    )
    session_dir = tmp_path / "sessions"
    child_pid, terminal_fd = pty.fork()
    if child_pid == 0:
        os.chdir(project)
        os.execve(
            sys.executable,
            [
                sys.executable,
                "-m",
                "wisp",
                "tui",
                "--session-dir",
                str(session_dir),
            ],
            {
                **os.environ,
                "WISP_PROVIDER": "fake",
                "WISP_MODEL": "",
                "WISP_RUST_TUI_BINARY": str(binary),
                "WISP_TRUST": "1",
            },
        )
    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 120, 0, 0))
    initial_terminal = termios.tcgetattr(terminal_fd)
    output = bytearray()
    status: int | None = None
    phase = "startup"
    context_redrawn = False
    deadline = time.monotonic() + 25
    next_browser_frame = 0.0
    browser_width = 121
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
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 121, 0, 0))
                context_redrawn = True
                output.clear()
                continue
            if phase == "startup" and context_redrawn and welcome_ready(output):
                os.write(terminal_fd, b"/skills\r")
                phase = "browser"
                output.clear()
            elif phase == "browser" and b"Skills" in output:
                phase = "browser ready"
                output.clear()
            elif (
                phase == "browser ready"
                and b"Inspect changes carefully" in output
                and b"Refreshing skills" not in output
            ):
                assert b"0-review" in output and b"project:wisp" in output
                os.write(terminal_fd, b"\r")
                phase = "inserted"
                output.clear()
            elif phase == "inserted" and b"/skill:0-review" in output:
                # Selecting only inserts the directive. The user supplies and submits
                # the request; the Python backend owns expansion and persistence.
                assert not list(session_dir.glob("*.jsonl"))
                os.write(terminal_fd, b"inspect this change\r")
                phase = "submitted"
                output.clear()
            elif phase == "submitted" and run_settled(output, 0, b"SKILL_EXPANSION_MARKER"):
                # FakeProvider echoes its expanded input as assistant output.
                os.write(terminal_fd, b"\x03")
                phase = "quit"
            if phase == "browser ready" and time.monotonic() >= next_browser_frame:
                # Cached entries are visible before refresh settles. Require a full
                # frame without the loading line before acting on its selection.
                browser_width += 1
                fcntl.ioctl(
                    terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 28, browser_width, 0, 0)
                )
                next_browser_frame = time.monotonic() + 0.1
                output.clear()
            waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                status = waited_status
                break
        if status is None:
            pytest.fail(f"Rust TUI timed out in {phase!r}; output={bytes(output)!r}")
        assert phase == "quit", (phase, bytes(output))
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
    entries = [
        json.loads(line)
        for path in session_dir.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    message = next(
        entry["message"]
        for entry in entries
        if entry.get("kind") == "message" and entry["message"]["role"] == "user"
    )
    assert message["skill_invocation"]["original_content"] == (
        "/skill:0-review inspect this change"
    )
    assert message["content"].startswith("[WISP EXPLICIT SKILL]\nSkill: 0-review")
    assert "SKILL_EXPANSION_MARKER" in message["content"]
    assert message["content"].endswith("[USER REQUEST]\ninspect this change")


@pytest.mark.process
def test_rust_file_picker_uses_python_discovery_and_submits_only_the_reference(
    tmp_path: Path,
) -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    relative_path = 'src/资料 "example".py'
    (project / relative_path).write_text("FILE_CONTENT_MUST_NOT_BE_INLINED", encoding="utf-8")
    protected_name = "PRIVATE_FILE_SHOULD_NOT_APPEAR.key"
    (project / protected_name).write_text("SECRET_MUST_NOT_APPEAR", encoding="utf-8")
    reference = "@" + json.dumps(relative_path, ensure_ascii=False)
    prompt = f"Explain {reference} "
    session_dir = tmp_path / "sessions"
    child_pid, terminal_fd = pty.fork()
    if child_pid == 0:
        os.chdir(project)
        os.execve(
            sys.executable,
            [
                sys.executable,
                "-m",
                "wisp",
                "tui",
                "--session-dir",
                str(session_dir),
            ],
            {
                **os.environ,
                "WISP_PROVIDER": "fake",
                "WISP_MODEL": "",
                "WISP_RUST_TUI_BINARY": str(binary),
                "WISP_TRUST": "1",
            },
        )
    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 120, 0, 0))
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
            # Ratatui can position over unchanged blanks and between wide glyphs.
            # Buffer tests assert exact layout; here compare the emitted nonblank
            # text, not an assumed contiguous ANSI write.
            emitted = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", output)
            compact = b"".join(emitted.split())
            if not context_redrawn and b"ctx" in output and b"~" in output:
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 121, 0, 0))
                context_redrawn = True
                output.clear()
                continue
            if (
                phase == "startup"
                and context_redrawn
                and welcome_ready(output)
                and (cursor := frame_cursor(output)) is not None
            ):
                # The welcome frame leaves the cursor at the start of the empty
                # composer. Later steps wait for the cursor to reach exactly the end
                # of what was typed or inserted, which proves every keystroke was
                # processed and painted.
                composer_start = cursor
                os.write(terminal_fd, b"Explain @")
                phase = "all files"
                output.clear()
            elif phase == "all files" and b"".join(relative_path.encode().split()) in compact:
                # The initial unfiltered list would expose this name if discovery
                # crossed the Python protected-path boundary.
                assert protected_name.encode() not in all_output
                os.write(terminal_fd, b"example")
                phase = "filtered"
                output.clear()
            elif (
                phase == "filtered"
                and b"".join(Path(relative_path).name.encode().split()) in compact
                and frame_cursor(output)
                == (composer_start[0], composer_start[1] + display_width("Explain @example"))
            ):
                # The picker ignores Enter until the selected row has been painted
                # for the complete query, so wait for the frame showing every typed
                # character instead of any frame that lists the file.
                os.write(terminal_fd, b"\r")
                phase = "inserted"
                output.clear()
            elif phase == "inserted" and frame_cursor(output) == (
                composer_start[0],
                composer_start[1] + display_width(prompt),
            ):
                assert not list(session_dir.glob("*.jsonl")), "selection must not submit"
                os.write(terminal_fd, b"\r")
                phase = "submitted"
                output.clear()
            elif phase == "submitted" and b"idle" in output:
                entries = [
                    entry
                    for path in session_dir.glob("*.jsonl")
                    for entry in complete_logged_commands(path)
                ]
                if any(
                    entry.get("kind") == "message"
                    and isinstance(message := entry.get("message"), dict)
                    and message.get("role") == "assistant"
                    for entry in entries
                ):
                    os.write(terminal_fd, b"\x03")
                    phase = "quit"
            waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                status = waited_status
                break
        if status is None:
            pytest.fail(f"Rust file picker timed out in {phase!r}; output={bytes(output)!r}")
        assert phase == "quit", (phase, bytes(output))
        assert os.waitstatus_to_exitcode(status) == 0, bytes(output)
        assert termios.tcgetattr(terminal_fd) == initial_terminal
        assert protected_name.encode() not in all_output
        assert b"SECRET_MUST_NOT_APPEAR" not in all_output
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
    messages = [
        entry["message"]
        for path in session_dir.glob("*.jsonl")
        for entry in complete_logged_commands(path)
        if entry.get("kind") == "message"
    ]
    user_messages = [
        message
        for message in messages
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    assert len(user_messages) == 1
    assert user_messages[0]["content"] == prompt
    assert "FILE_CONTENT_MUST_NOT_BE_INLINED" not in json.dumps(messages)
    # The autouse fixture's empty parent project must remain untouched.
    assert not (Path.cwd() / ".wisp").exists()
