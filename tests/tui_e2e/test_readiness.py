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

from tests.tui_e2e.support import (
    full_repaint_after,
    rendered_without_spacing,
    run_settled,
    screen_shows,
    welcome_ready,
)


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

    def submit(self, predicate: Callable[[bytes], bool], *, failure: str) -> None:
        """Press Enter until ``predicate`` holds, again while a refresh refuses it.

        After a run the TUI refreshes session metadata, and a submit made during
        that refresh is refused with a notice and stays in the composer.
        """
        self.send(b"\r")
        last_submit = time.monotonic()

        def settled(output: bytes) -> bool:
            nonlocal last_submit
            if predicate(output):
                return True
            if time.monotonic() - last_submit > 0.5 and screen_shows(
                output, "metadata refresh to finish"
            ):
                self.send(b"\r")
                last_submit = time.monotonic()
            return False

        self.wait_until(settled, failure=failure)

    def wait_for(self, *markers: bytes, since: int = 0, failure: str) -> bytes:
        return self.wait_until(
            lambda output: all(marker in output[since:] for marker in markers),
            failure=failure,
        )

    def wait_ready(self) -> None:
        self.wait_for(b"ctx", b"~", failure="Rust TUI context status did not render")
        self.resize(width=101)
        offset = len(self.output)
        # The composer border styles the model and mode apart, so read it from the
        # replayed screen rather than the raw bytes.
        self.wait_until(
            lambda output: welcome_ready(output[offset:]) and screen_shows(output, "─ fake "),
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
        # A killed TUI can stall mid-exit on macOS until the other end of its
        # terminal closes, so close it before waiting.
        os.close(self.fd)
        if self.status is None:
            deadline = time.monotonic() + 10
            while os.waitpid(self.pid, os.WNOHANG)[0] == 0:
                if time.monotonic() >= deadline:
                    pytest.fail(f"Rust TUI {self.pid} did not exit after SIGKILL")
                time.sleep(0.05)


def _launch(
    tmp_path: Path,
    *,
    home: Path,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[_TuiProcess, Path]:
    session_dir = tmp_path / "sessions"
    binary = _rust_binary()
    child_pid, terminal_fd = pty.fork()
    if child_pid == 0:
        if cwd is not None:
            os.chdir(cwd)
        environment = {
            **os.environ,
            "WISP_RUST_TUI_BINARY": str(binary),
            "WISP_TRUST": "1",
            "HOME": str(home),
            "USERPROFILE": str(home),
        }
        for name in ("WISP_PROVIDER", "WISP_MODEL", "WISP_RUST_TUI_BINDINGS_JSON"):
            environment.pop(name, None)
        environment.update(env or {})
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


# Runs of dots that only the finished Wisp logo shows, at its mini, normal and full sizes.
_FINISHED_LOGO_ROWS = ("⢀⠤⠶⠶⣿⣿⡄", "⣿⣿⣿⣿⣿⣿⣿⣿⣁⣠⡿", "⣾⣿⣿⣿⣿⣿⣿⣿⣿⡿⠋")


@pytest.mark.process
def test_logo_entrance_settles_without_input(tmp_path: Path) -> None:
    """The welcome logo's entrance finishes on its own, then the screen stops redrawing."""
    home = tmp_path / "home"
    _write_user_settings(home, provider="fake")
    (home / ".wisp" / "tui.json").write_text(
        json.dumps({"startup_logo": "wisp-braille"}), encoding="utf-8"
    )
    tui, _ = _launch(tmp_path, home=home, env={"COLORTERM": "truecolor"})
    try:
        tui.wait_until(
            lambda output: (
                welcome_ready(output)
                and any(screen_shows(output, row) for row in _FINISHED_LOGO_ROWS)
            ),
            failure="the welcome logo did not finish without input",
        )
        # Past the entrance's 1.25 s, an idle welcome screen writes nothing more.
        settle_until = time.monotonic() + 1.5
        tui.wait_until(lambda _output: time.monotonic() >= settle_until, failure="settling")
        quiet_from = len(tui.output)
        quiet_until = time.monotonic() + 1.5
        tui.wait_until(lambda _output: time.monotonic() >= quiet_until, failure="quiet")
        assert len(tui.output) == quiet_from, bytes(tui.output[quiet_from:])
        tui.quit()
    finally:
        tui.close()


@pytest.mark.process
def test_init_command_is_discovered_and_runs_through_the_backend_lifecycle(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("# Fixture project\n", encoding="utf-8")
    _write_user_settings(home, provider="fake")
    tui, session_dir = _launch(tmp_path, home=home, cwd=project)
    try:
        tui.wait_ready()

        offset = len(tui.output)
        tui.send(b"/init extra\r")
        tui.wait_for(
            b"Usage: /init",
            since=offset,
            failure="Rust TUI did not reject /init arguments",
        )

        offset = len(tui.output)
        tui.send(b"\x15/init\r")
        tui.wait_until(
            lambda output: run_settled(output, offset, b"fake", b"response", b"/init", b"failed"),
            failure="Rust TUI did not settle the backend-owned initialization lifecycle",
        )

        tui.send(b"after init")
        # Typed characters can reach the screen across several frames.
        tui.wait_until(
            lambda output: screen_shows(output, "after init"),
            failure="Rust TUI did not restore composer editing after initialization failed",
        )
        tui.quit()
    finally:
        tui.close()

    messages = _conversation_messages(session_dir)
    assert messages[0] == ("user", "/init")
    assert messages[1][0] == "assistant"
    assert not (project / "AGENTS.md").exists()


def _editor_script(tmp_path: Path, body: str) -> Path:
    """Write an executable stand-in for the user's editor; the draft file is ``$1``."""
    editor = tmp_path / "editor.sh"
    editor.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    editor.chmod(0o755)
    return editor


def _open_editor(tui: _TuiProcess, draft: bytes) -> None:
    """Type a draft, open the editor with Ctrl+O and wait until the TUI steps aside."""
    tui.send(draft)
    tui.wait_until(
        lambda output: screen_shows(output, draft.decode()),
        failure="Rust TUI did not show the typed draft",
    )
    offset = len(tui.output)
    tui.send(b"\x0f")
    # Leaving the alternate screen is the last thing the TUI does before the editor runs.
    tui.wait_for(b"\x1b[?1049l", since=offset, failure="Rust TUI did not hand over the terminal")


def _send_settled(tui: _TuiProcess) -> None:
    offset = len(tui.output)
    tui.send(b"\r")
    tui.wait_until(
        lambda output: run_settled(output, offset, b"fake", b"response"),
        failure="Rust TUI did not send the draft",
    )


@pytest.mark.process
def test_external_editor_replaces_the_draft(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_user_settings(home, provider="fake")
    # The editor proves it received the draft, then reads its new text from the
    # terminal. Only a foreground job may read it, and only if the TUI has stopped
    # reading, so the typed line arriving proves both.
    editor = _editor_script(
        tmp_path,
        "grep -q 'typed draft' \"$1\" || exit 1\n"
        'read line </dev/tty\nprintf \'%s\\n\' "$line" >"$1"',
    )
    tui, session_dir = _launch(tmp_path, home=home, env={"VISUAL": str(editor)})
    try:
        tui.wait_ready()
        _open_editor(tui, b"typed draft")
        tui.send(b"from the editor\r")
        tui.wait_until(
            lambda output: screen_shows(output, "from the editor"),
            failure="Rust TUI did not load the draft the editor saved",
        )
        # The TUI reads the keyboard again once the editor exits.
        tui.send(b" and more")
        tui.wait_until(
            lambda output: screen_shows(output, "from the editor and more"),
            failure="Rust TUI did not take keyboard input back after the editor",
        )
        _send_settled(tui)
        tui.quit()
    finally:
        tui.close()

    messages = _conversation_messages(session_dir)
    assert messages[0] == ("user", "from the editor and more")


@pytest.mark.process
def test_ctrl_c_in_editor_cancels_only_the_edit(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_user_settings(home, provider="fake")
    # The first read proves the editor holds the terminal's foreground; Ctrl+C then
    # interrupts only the editor, as it would a shell job.
    editor = _editor_script(
        tmp_path,
        "read line </dev/tty\nprintf 'EDITOR READY\\n' >/dev/tty\n"
        "read line </dev/tty\nprintf 'lost' >\"$1\"",
    )
    tui, session_dir = _launch(tmp_path, home=home, env={"VISUAL": str(editor)})
    try:
        tui.wait_ready()
        _open_editor(tui, b"typed draft")
        offset = len(tui.output)
        tui.send(b"go\r")
        tui.wait_for(b"EDITOR READY", since=offset, failure="the editor did not take the terminal")
        tui.send(b"\x03")
        tui.wait_until(
            lambda output: screen_shows(output, "Draft unchanged"),
            failure="Rust TUI did not report the interrupted edit",
        )
        tui.send(b" still here")
        tui.wait_until(
            lambda output: screen_shows(output, "typed draft still here"),
            failure="Rust TUI did not keep the draft and its keyboard after Ctrl+C",
        )
        _send_settled(tui)
        tui.quit()
    finally:
        tui.close()

    messages = _conversation_messages(session_dir)
    assert messages[0] == ("user", "typed draft still here")


@pytest.mark.process
def test_suspended_editor_is_resumed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_user_settings(home, provider="fake")
    # Stopping the whole job is what an editor's Ctrl+Z does (vim sends SIGTSTP to its
    # process group). The TUI continues it rather than waiting forever.
    editor = _editor_script(tmp_path, "kill -STOP 0\nprintf 'resumed\\n' >\"$1\"")
    tui, _session_dir = _launch(tmp_path, home=home, env={"VISUAL": str(editor)})
    try:
        tui.wait_ready()
        _open_editor(tui, b"typed draft")
        tui.wait_until(
            lambda output: screen_shows(output, "resumed"),
            failure="Rust TUI did not resume a stopped editor",
        )
        _send_settled(tui)
        tui.quit()
    finally:
        tui.close()


@pytest.mark.process
def test_terminated_launcher_stops_the_editor(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_user_settings(home, provider="fake")
    draft_dir = tmp_path / "drafts"
    draft_dir.mkdir()
    pid_file = tmp_path / "editor.pid"
    # The editor runs in its own process group, outside the group the launcher
    # supervises, so terminating the launcher must still reach it.
    editor = _editor_script(tmp_path, f"echo $$ >'{pid_file}'\nexec sleep 60")
    tui, _session_dir = _launch(
        tmp_path, home=home, env={"VISUAL": str(editor), "TMPDIR": str(draft_dir)}
    )
    try:
        tui.wait_ready()
        _open_editor(tui, b"typed draft")
        tui.wait_until(
            lambda _output: pid_file.exists() and pid_file.read_text().strip() != "",
            failure="the editor did not start",
        )
        editor_pid = int(pid_file.read_text())
        os.kill(tui.pid, signal.SIGTERM)
        tui.wait_until(
            lambda _output: tui.status is not None,
            failure="the launcher did not exit after SIGTERM",
        )
        deadline = time.monotonic() + 5
        while _process_alive(editor_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _process_alive(editor_pid), "the editor outlived the launcher"
        assert list(draft_dir.glob("wisp-prompt-*")) == []
        assert termios.tcgetattr(tui.fd) == tui.initial_terminal
    finally:
        tui.close()


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.process
def test_shift_enter_inserts_newline(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_user_settings(home, provider="fake")
    tui, session_dir = _launch(tmp_path, home=home)
    try:
        tui.wait_ready()
        # The TUI asks for the kitty keyboard protocol, so a terminal that speaks it
        # reports Shift+Enter apart from Enter.
        assert b"\x1b[>1u" in tui.output
        offset = len(tui.output)
        tui.send(b"first\x1b[13;2usecond")
        tui.wait_until(
            lambda output: screen_shows(output, "second"),
            failure="Shift+Enter did not keep the draft open for a second line",
        )
        offset = len(tui.output)
        tui.send(b"\r")
        tui.wait_until(
            lambda output: run_settled(output, offset, b"fake", b"response", b"to:"),
            failure="two-line prompt did not complete",
        )
        tui.quit()
        # Leaving the TUI hands the terminal back with the protocol popped.
        assert b"\x1b[<1u" in tui.output
    finally:
        tui.close()

    assert _conversation_messages(session_dir) == [
        ("user", "first\nsecond"),
        ("assistant", "fake response to: first\nsecond"),
    ]


@pytest.mark.process
def test_ctrl_r_restores_prompt_without_submitting(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_user_settings(home, provider="fake")
    tui, session_dir = _launch(tmp_path, home=home)
    try:
        tui.wait_ready()
        offset = len(tui.output)
        tui.send(b"recall me\r")
        tui.wait_until(
            lambda output: run_settled(output, offset, b"fake", b"response", b"to:"),
            failure="first prompt did not complete",
        )
        tui.send(b"\x12")
        tui.wait_for(b"Enter restore", failure="history did not open")
        # Enter acts only on a choice that has been painted; a full repaint proves it.
        offset = len(tui.output)
        tui.resize(width=102)
        tui.wait_until(
            lambda output: b"Enter restore" in (full_repaint_after(output, offset) or b""),
            failure="history did not repaint",
        )
        tui.send(b"\r")
        # The transcript shows the same words, so wait for the popup to close with the
        # draft behind the composer's prompt mark.
        tui.wait_until(
            lambda output: (
                screen_shows(output, "› recall me") and not screen_shows(output, "Enter restore")
            ),
            failure="history did not restore the prompt into the composer",
        )
        offset = len(tui.output)
        # The first run's metadata refresh may still be running.
        tui.submit(
            lambda output: run_settled(output, offset, b"fake", b"response", b"to:"),
            failure="restored prompt did not complete",
        )
        tui.quit()
    finally:
        tui.close()

    # Exactly two runs: restoring the prompt must not have submitted it.
    exchange = [("user", "recall me"), ("assistant", "fake response to: recall me")]
    assert _conversation_messages(session_dir) == exchange * 2


@pytest.mark.process
def test_composer_selection_and_word_deletion_submit_exact_unicode_text(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_user_settings(home, provider="fake")
    tui, session_dir = _launch(tmp_path, home=home)
    prefix = "keep e\u0301👩🏽‍💻 "
    try:
        tui.wait_ready()
        offset = len(tui.output)
        tui.send(b"\x1b[200~" + (prefix + "old").encode() + b"\x1b[201~")
        tui.wait_for(b"old", since=offset, failure="initial Unicode draft did not render")

        # Ctrl+Shift+Left selects the last word; bracketed paste replaces it.
        offset = len(tui.output)
        tui.send(b"\x1b[1;6D")
        tui.wait_for(b"old", since=offset, failure="selected word did not repaint")
        offset = len(tui.output)
        tui.send(b"\x1b[200~new\nline\x1b[201~")
        tui.wait_for(
            b"line", since=offset, failure="multiline selection replacement did not render"
        )

        offset = len(tui.output)
        # Check the persisted submission: differential redraws may reuse the
        # letters shared by "line" and "final" instead of emitting the word.
        tui.send(b"\x17final\r")  # Ctrl+W deletes the word on the second line.
        tui.wait_until(
            lambda output: run_settled(output, offset, b"fake", b"response", b"to:"),
            failure="edited prompt did not complete",
        )
        tui.quit()
    finally:
        tui.close()

    expected = prefix + "new\nfinal"
    assert _conversation_messages(session_dir) == [
        ("user", expected),
        ("assistant", "fake response to: " + expected),
    ]


@pytest.mark.process
def test_composer_undo_redo_keys_submit_only_the_current_edit_branch(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_user_settings(home, provider="fake")
    tui, session_dir = _launch(tmp_path, home=home)
    try:
        tui.wait_ready()
        offset = len(tui.output)
        tui.send(b"typed")
        # Keys read in separate wake-ups paint in separate frames ("t", then "yped"),
        # and the welcome menu repaints between them as it steps back for the draft,
        # so read the replayed screen rather than the painted bytes.
        tui.wait_until(
            lambda output: screen_shows(output, "› typed"),
            failure="typed undo group did not render",
        )

        offset = len(tui.output)
        tui.send(b"\x1a")  # Ctrl+Z
        # Resize only after the undo paints the empty-composer placeholder; a resize
        # ready in the same input wake-up can leave the key unread (crossterm's
        # input sources are edge-triggered).
        tui.wait_until(
            lambda output: b"AskWispanything" in rendered_without_spacing(output[offset:]),
            failure="undo did not clear the composer",
        )
        tui.resize(width=102)
        tui.wait_until(
            lambda output: welcome_ready(output[offset:]),
            failure="undo did not restore the empty composer",
        )

        offset = len(tui.output)
        tui.send(b"\x19")  # Ctrl+Y
        tui.wait_until(
            lambda output: screen_shows(output, "› typed"),
            failure="redo did not restore the edit group",
        )

        tui.send(b"\x1afinal\x19\r")
        tui.wait_until(
            lambda output: run_settled(output, offset, b"fake", b"response", b"to:"),
            failure="branched undo draft did not complete",
        )
        tui.quit()
    finally:
        tui.close()

    assert _conversation_messages(session_dir) == [
        ("user", "final"),
        ("assistant", "fake response to: final"),
    ]


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
            lambda output: run_settled(output, offset, b"fake", b"response", b"to:"),
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
            lambda output: run_settled(output, offset, b"fake", b"response", b"to:"),
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
        # Do not require a fresh "idle" after the submit: the fake reply can finish before
        # the running footer is painted, and an unchanged footer is never redrawn. With the reply
        # painted, run_settled's last-idle-after-last-working check proves the run ended.
        tui.wait_until(
            lambda output: run_settled(output, offset, b"END", *marker_fragments),
            timeout=20,
            failure="large pasted prompt did not complete with compact echo",
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
            lambda output: _has_fragments(output, offset, b"Keys", b"Undo:", b"Ctrl+Z", b"Redo:"),
            failure="contextual key help did not open",
        )

        offset = len(tui.output)
        tui.send(b"\x1b")
        # The draft is also visible beneath help. Wait for the replayed screen to
        # show the welcome without help before resizing or sending input to the
        # composer; closing repaints only the cells help covered.
        tui.wait_until(
            lambda output: (
                screen_shows(output, "Browse commands") and not screen_shows(output, "Undo:")
            ),
            failure="contextual key help did not close",
        )
        offset = len(tui.output)
        tui.resize(width=102)
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
