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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest


def _rust_binary() -> Path:
    value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    return Path(value).resolve(strict=True)


def _write_user_settings(home: Path, **values: object) -> None:
    settings_path = home / ".wisp" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(values), encoding="utf-8")


@dataclass
class _TuiProcess:
    pid: int
    fd: int
    initial_terminal: list[int | list[bytes]]
    output: bytearray = field(default_factory=bytearray)
    status: int | None = None

    def send(self, value: bytes) -> None:
        os.write(self.fd, value)

    def resize(self, *, width: int) -> None:
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, width, 0, 0))

    def wait_until(
        self,
        predicate: Callable[[bytes], bool],
        *,
        timeout: float = 15,
        failure: str,
    ) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            readable, _, _ = select.select([self.fd], [], [], 0.05)
            if readable:
                try:
                    self.output.extend(os.read(self.fd, 65536))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            if self.status is None:
                waited_pid, waited_status = os.waitpid(self.pid, os.WNOHANG)
                if waited_pid == self.pid:
                    self.status = waited_status
            # Poll once before checking the condition. A reaped exit can satisfy
            # quit's condition rather than falling through as an unexpected exit.
            if predicate(bytes(self.output)):
                return bytes(self.output)
            if self.status is not None:
                break
        pytest.fail(f"{failure}; terminal tail={bytes(self.output[-8000:])!r}")

    def wait_for(self, *markers: bytes, since: int = 0, failure: str) -> bytes:
        return self.wait_until(
            lambda output: all(marker in output[since:] for marker in markers),
            failure=failure,
        )

    def wait_ready(self) -> None:
        self.wait_for(b"ctx", b"~", failure="Rust TUI context status did not render")
        self.resize(width=101)
        offset = len(self.output)
        self.wait_for(
            b"Type a prompt or / for commands.",
            b"fake/fake",
            since=offset,
            failure="Rust TUI did not finish startup hydration",
        )

    def quit(self) -> None:
        self.send(b"\x03")
        self.wait_until(
            lambda _output: self.status is not None,
            failure="Rust TUI did not exit after Ctrl+C",
        )
        assert self.status is not None
        assert os.waitstatus_to_exitcode(self.status) == 0, bytes(self.output)
        assert termios.tcgetattr(self.fd) == self.initial_terminal

    def close(self) -> None:
        if self.status is None:
            try:
                os.killpg(os.tcgetpgrp(self.fd), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(self.pid, 0)
        os.close(self.fd)


def _launch(tmp_path: Path, *, home: Path) -> tuple[_TuiProcess, Path]:
    session_dir = tmp_path / "sessions"
    binary = _rust_binary()
    child_pid, terminal_fd = pty.fork()
    if child_pid == 0:
        environment = {
            **os.environ,
            "WISP_RUST_TUI_BINARY": str(binary),
            "WISP_TRUST": "1",
            "HOME": str(home),
            "USERPROFILE": str(home),
        }
        for name in ("WISP_PROVIDER", "WISP_MODEL", "WISP_RUST_TUI_BINDINGS_JSON"):
            environment.pop(name, None)
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
            environment,
        )
    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
    return (
        _TuiProcess(
            pid=child_pid,
            fd=terminal_fd,
            initial_terminal=termios.tcgetattr(terminal_fd),
        ),
        session_dir,
    )


def _conversation_messages(session_dir: Path) -> list[tuple[str, str]]:
    entries = [
        json.loads(line)
        for path in session_dir.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    return [
        (message["role"], message["content"])
        for entry in entries
        if entry.get("kind") == "message"
        and (message := entry["message"])["role"] in {"user", "assistant"}
    ]


def _has_fragments(output: bytes, offset: int, *fragments: bytes) -> bool:
    phase_output = output[offset:]
    return all(fragment in phase_output for fragment in fragments)


@pytest.mark.process
def test_launcher_applies_custom_submit_and_removes_old_enter_binding(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_user_settings(
        home,
        provider="fake",
        tui_keybindings={"prompt.submit": ["f3"]},
    )
    tui, session_dir = _launch(tmp_path, home=home)
    try:
        tui.wait_ready()
        tui.send(b"launch binding\r")
        time.sleep(0.2)
        tui.resize(width=102)
        offset = len(tui.output)
        tui.wait_for(
            b"launch binding",
            since=offset,
            failure="unbound Enter changed or lost the draft",
        )
        assert not list(session_dir.glob("*.jsonl")), "unbound Enter dispatched a prompt"

        offset = len(tui.output)
        tui.send(b"\x1bOR")  # xterm F3
        tui.wait_until(
            lambda output: (
                _has_fragments(output, offset, b"fake", b"response", b"to:")
                and output.rfind(b"idle") > output.rfind(b"working")
            ),
            failure="custom F3 submit did not complete",
        )
        tui.quit()
    finally:
        tui.close()

    assert _conversation_messages(session_dir) == [
        ("user", "launch binding"),
        ("assistant", "fake response to: launch binding"),
    ]


@pytest.mark.process
def test_invalid_binding_settings_keep_provider_and_restore_defaults(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_user_settings(
        home,
        provider="fake",
        tui_keybindings={"prompt.submit": "not-a-list"},
    )
    tui, session_dir = _launch(tmp_path, home=home)
    try:
        tui.wait_ready()
        assert b"ignoring invalid tui_keybindings" in tui.output
        offset = len(tui.output)
        tui.send(b"defaults recovered\r")
        tui.wait_until(
            lambda output: (
                _has_fragments(output, offset, b"fake", b"response", b"to:")
                and output.rfind(b"idle") > output.rfind(b"working")
            ),
            failure="default Enter submit did not recover",
        )
        tui.quit()
    finally:
        tui.close()

    assert _conversation_messages(session_dir) == [
        ("user", "defaults recovered"),
        ("assistant", "fake response to: defaults recovered"),
    ]


@pytest.mark.process
def test_unicode_multiline_large_paste_is_compact_but_submits_exact_text(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_user_settings(home, provider="fake")
    tui, session_dir = _launch(tmp_path, home=home)
    prompt = f"{'界' * 2001}\n🙂END"
    marker_fragments = (b"[Pasted", b"content", b"#1:", b"2006", b"6011", b"bytes]")
    try:
        tui.wait_ready()
        offset = len(tui.output)
        tui.send(b"\x1b[200~" + prompt.encode() + b"\x1b[201~")
        tui.wait_until(
            lambda output: _has_fragments(output, offset, *marker_fragments),
            failure="large paste did not render compactly",
        )

        offset = len(tui.output)
        tui.send(b"\r")
        tui.wait_until(
            lambda output: (
                _has_fragments(output, offset, b"idle", b"END")
                and output.rfind(b"idle") > output.rfind(b"working")
            ),
            timeout=20,
            failure="large pasted prompt did not complete",
        )
        # The fake assistant echoes the full prompt, scrolling the user entry off
        # screen. Return to the oldest transcript rows to inspect its live echo.
        offset = len(tui.output)
        tui.send(b"\x1b[1;5H")  # Ctrl+Home
        tui.wait_until(
            lambda output: _has_fragments(output, offset, *marker_fragments),
            failure="submitted user message did not retain its compact presentation",
        )
        tui.quit()
    finally:
        tui.close()

    assert _conversation_messages(session_dir) == [
        ("user", prompt),
        ("assistant", f"fake response to: {prompt}"),
    ]


@pytest.mark.process
def test_context_help_preserves_draft_and_update_is_local_guidance(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_user_settings(home, provider="fake")
    tui, session_dir = _launch(tmp_path, home=home)
    try:
        tui.wait_ready()
        tui.send(b"keep draft")
        offset = len(tui.output)
        tui.send(b"\x07")  # Ctrl+G
        tui.wait_until(
            lambda output: _has_fragments(
                output, offset, b"Keys", b"Large-paste", b"markers", b"expand"
            ),
            failure="contextual key help did not open",
        )

        tui.send(b"\x1b")
        tui.resize(width=102)
        offset = len(tui.output)
        tui.wait_for(
            b"keep draft",
            since=offset,
            failure="closing contextual help did not restore the draft",
        )

        tui.send(b"\x7f" * len("keep draft"))
        offset = len(tui.output)
        tui.send(b"/update check\r")
        tui.wait_until(
            lambda output: _has_fragments(
                output,
                offset,
                b"External",
                b"update",
                b"only:",
                b"wisp",
                b"--check",
            ),
            failure="update guidance did not render",
        )
        assert os.waitpid(tui.pid, os.WNOHANG) == (0, 0), "/update exited the TUI"
        assert not list(session_dir.glob("*.jsonl")), "/update dispatched backend work"
        tui.quit()
    finally:
        tui.close()


def test_wait_until_accepts_an_exit_reaped_after_the_first_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tui = _TuiProcess(pid=17, fd=3, initial_terminal=[])
    polls = iter([(0, 0), (17, 0)])
    monkeypatch.setattr(select, "select", lambda *_args: ([], [], []))
    monkeypatch.setattr(os, "waitpid", lambda *_args: next(polls))

    tui.wait_until(lambda _output: tui.status is not None, failure="expected clean exit")

    assert tui.status == 0
