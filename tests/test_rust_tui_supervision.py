from __future__ import annotations

import io
import signal
import subprocess
import termios
from contextlib import nullcontext
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from wisp.config import WispConfig
from wisp.tui import rust_launcher
from wisp.tui.launch import TuiOptions
from wisp.tui.rust_launcher import RustTuiLaunchError, _TerminalSnapshot


class _FakeProcess:
    pid = 4321

    def __init__(self, *, wait_result: int = 0, interrupt: bool = False) -> None:
        self.wait_result = wait_result
        self.interrupt = interrupt
        self.poll_count = 0

    def wait(self) -> int:
        return self.wait_result

    def poll(self) -> int | None:
        if self.interrupt:
            raise KeyboardInterrupt
        self.poll_count += 1
        return self.wait_result


def _options(tmp_path: Path) -> TuiOptions:
    return TuiOptions(config=WispConfig(provider="fake", session_dir=tmp_path))


def test_cleanup_escalates_graceful_then_term_then_kill(monkeypatch: MonkeyPatch) -> None:
    process = _FakeProcess()
    waits = iter((False, False, True))
    signals: list[int] = []
    monkeypatch.setattr(rust_launcher.os, "getpgrp", lambda: 99)
    monkeypatch.setattr(rust_launcher, "_process_group_exists", lambda _pgid: True)
    monkeypatch.setattr(
        rust_launcher,
        "_wait_for_process_group",
        lambda *_args: next(waits),
    )
    monkeypatch.setattr(
        rust_launcher,
        "_signal_process_group",
        lambda _pgid, signum: signals.append(signum),
    )

    rust_launcher._cleanup_process_group(process, graceful_signal=signal.SIGINT)  # type: ignore[arg-type]

    assert signals == [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]


def test_cleanup_reports_kill_timeout(monkeypatch: MonkeyPatch) -> None:
    process = _FakeProcess()
    monkeypatch.setattr(rust_launcher.os, "getpgrp", lambda: 99)
    monkeypatch.setattr(rust_launcher, "_wait_for_process_group", lambda *_args: False)
    monkeypatch.setattr(rust_launcher, "_signal_process_group", lambda *_args: None)

    with pytest.raises(RustTuiLaunchError, match="did not exit after SIGKILL"):
        rust_launcher._cleanup_process_group(process, graceful_signal=None)  # type: ignore[arg-type]


def test_terminal_restoration_restores_pgrp_termios_and_ansi_baseline(
    monkeypatch: MonkeyPatch,
) -> None:
    attributes: list[int | list[bytes]] = [0, 0, 0, 0, 0, 0, [b"\x00"]]
    snapshot = _TerminalSnapshot(fd=7, foreground_pgrp=81, attributes=attributes)
    foreground: list[tuple[int, int]] = []
    restored: list[tuple[int, int, list[int | list[bytes]]]] = []
    output = io.StringIO()
    monkeypatch.setattr(
        rust_launcher,
        "_set_foreground_pgrp",
        lambda fd, pgrp: foreground.append((fd, pgrp)),
    )
    monkeypatch.setattr(
        rust_launcher.termios,
        "tcsetattr",
        lambda fd, when, attrs: restored.append((fd, when, attrs)),
    )
    monkeypatch.setattr(rust_launcher.sys, "stdout", output)

    rust_launcher._restore_terminal(snapshot)

    assert foreground == [(7, 81)]
    assert restored == [(7, termios.TCSANOW, attributes)]
    assert output.getvalue() == rust_launcher._ANSI_BASELINE
    assert output.getvalue().endswith("\x1b[0m\x1b[?25h")


def test_keyboard_interrupt_cleans_group_and_restores_terminal(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    process = _FakeProcess(interrupt=True)
    snapshot = _TerminalSnapshot(
        fd=7,
        foreground_pgrp=81,
        attributes=[0, 0, 0, 0, 0, 0, [b"\x00"]],
    )
    cleanup_signals: list[int | None] = []
    restored: list[_TerminalSnapshot | None] = []

    async def fake_preflight(_options: TuiOptions) -> None:
        return None

    monkeypatch.setattr(rust_launcher, "resolve_rust_tui_binary", lambda: Path("/wisp-tui"))
    monkeypatch.setattr(rust_launcher, "_preflight_tui_options", fake_preflight)
    monkeypatch.setattr(rust_launcher.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(rust_launcher, "_snapshot_terminal", lambda: snapshot)
    monkeypatch.setattr(rust_launcher, "_set_foreground_pgrp", lambda *_args: None)
    monkeypatch.setattr(
        rust_launcher,
        "_supervisor_signal_handlers",
        lambda: nullcontext(rust_launcher._SupervisorSignals()),
    )
    monkeypatch.setattr(
        rust_launcher,
        "_cleanup_process_group",
        lambda _process, *, graceful_signal: cleanup_signals.append(graceful_signal),
    )
    monkeypatch.setattr(rust_launcher, "_restore_terminal", restored.append)

    assert rust_launcher.run_rust_tui(_options(tmp_path)) == 128 + signal.SIGINT
    assert cleanup_signals == [signal.SIGINT]
    assert restored == [snapshot]


def test_spawn_failure_still_restores_terminal(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    snapshot = _TerminalSnapshot(
        fd=7,
        foreground_pgrp=81,
        attributes=[0, 0, 0, 0, 0, 0, [b"\x00"]],
    )
    restored: list[_TerminalSnapshot | None] = []

    async def fake_preflight(_options: TuiOptions) -> None:
        return None

    def fail_spawn(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
        raise OSError("exec format error")

    monkeypatch.setattr(rust_launcher, "resolve_rust_tui_binary", lambda: Path("/wisp-tui"))
    monkeypatch.setattr(rust_launcher, "_preflight_tui_options", fake_preflight)
    monkeypatch.setattr(rust_launcher.subprocess, "Popen", fail_spawn)
    monkeypatch.setattr(rust_launcher, "_snapshot_terminal", lambda: snapshot)
    monkeypatch.setattr(rust_launcher, "_restore_terminal", restored.append)

    with pytest.raises(RustTuiLaunchError, match="failed to start Rust TUI binary"):
        rust_launcher.run_rust_tui(_options(tmp_path))

    assert restored == [snapshot]


def test_supervisor_signal_handlers_record_interrupt_without_unwinding(
    monkeypatch: MonkeyPatch,
) -> None:
    installed: dict[int, object] = {}

    def install(signum: int, handler: object) -> object:
        previous = installed.get(signum, signal.SIG_DFL)
        installed[signum] = handler
        return previous

    monkeypatch.setattr(rust_launcher.signal, "signal", install)

    with rust_launcher._supervisor_signal_handlers() as observed:
        handler = installed[signal.SIGTERM]
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert observed.signum == signal.SIGTERM


def test_signal_received_during_cleanup_overrides_success(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    process = _FakeProcess()
    observed = rust_launcher._SupervisorSignals()

    async def fake_preflight(_options: TuiOptions) -> None:
        return None

    def cleanup(*_args: object, **_kwargs: object) -> None:
        observed.signum = signal.SIGTERM

    monkeypatch.setattr(rust_launcher, "resolve_rust_tui_binary", lambda: Path("/wisp-tui"))
    monkeypatch.setattr(rust_launcher, "_preflight_tui_options", fake_preflight)
    monkeypatch.setattr(rust_launcher.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(rust_launcher, "_snapshot_terminal", lambda: None)
    monkeypatch.setattr(
        rust_launcher,
        "_supervisor_signal_handlers",
        lambda: nullcontext(observed),
    )
    monkeypatch.setattr(rust_launcher, "_cleanup_process_group", cleanup)

    assert rust_launcher.run_rust_tui(_options(tmp_path)) == 128 + signal.SIGTERM
