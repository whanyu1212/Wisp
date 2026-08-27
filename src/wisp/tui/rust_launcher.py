"""Launch and supervise Wisp's optional Rust terminal frontend."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import termios
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

import anyio

from wisp import __version__
from wisp.tui.launch import TuiOptions, _preflight_tui_options, _rpc_command, _rpc_env

_BINARY_ENV = "WISP_RUST_TUI_BINARY"
_PACKAGED_BINARY = Path(__file__).resolve().parent / "bin" / "wisp-tui"
_GRACE_SECONDS = 1.0
_TERM_SECONDS = 1.0
_KILL_SECONDS = 1.0
_POLL_SECONDS = 0.01
_ANSI_BASELINE = (
    "\x1b[?2026l\x1b[?1049l\x1b[?2004l\x1b[?1000l\x1b[?1002l"
    "\x1b[?1003l\x1b[?1006l\x1b[?1004l\x1b[0m\x1b[?25h"
)


class RustTuiLaunchError(RuntimeError):
    """The explicitly selected Rust frontend could not be launched safely."""


@dataclass(frozen=True)
class _TerminalSnapshot:
    fd: int
    foreground_pgrp: int
    attributes: list[int | list[bytes]]


@dataclass
class _SupervisorSignals:
    signum: int | None = None


def resolve_rust_tui_binary() -> Path:
    """Resolve only a trusted package path or an absolute development override."""

    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        raise RustTuiLaunchError(
            "the Rust TUI is currently supported only on macOS and Linux; "
            "use `wisp tui --renderer textual` on Windows"
        )

    override = os.environ.get(_BINARY_ENV)
    if override is not None:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise RustTuiLaunchError(f"{_BINARY_ENV} must be an absolute executable path")
    else:
        path = _PACKAGED_BINARY

    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        source = _BINARY_ENV if override is not None else "the installed Wisp package"
        raise RustTuiLaunchError(
            f"Rust TUI binary was not found via {source}; "
            f"set {_BINARY_ENV} to an absolute development binary path"
        ) from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise RustTuiLaunchError(f"Rust TUI binary is not executable: {resolved}")
    return resolved


def rust_tui_command(binary: Path, options: TuiOptions) -> tuple[str, ...]:
    """Build the Rust argv while leaving the backend argv opaque after ``--``."""

    return (
        str(binary),
        "--expected-backend-version",
        __version__,
        "--",
        *_rpc_command(options),
    )


def run_rust_tui(options: TuiOptions) -> int:
    """Preflight, launch, and supervise the Rust frontend and Python backend."""

    binary = resolve_rust_tui_binary()
    anyio.run(_preflight_tui_options, options)
    terminal = _snapshot_terminal()
    process: subprocess.Popen[bytes] | None = None
    cleanup_error: RustTuiLaunchError | None = None
    status = 1
    graceful_signal: int | None = None
    with _supervisor_signal_handlers() as supervisor_signals:
        try:
            try:
                if supervisor_signals.signum is not None:
                    graceful_signal = supervisor_signals.signum
                    status = 128 + graceful_signal
                else:
                    environment = _rpc_env(options)
                    environment.pop(_BINARY_ENV, None)
                    process = subprocess.Popen(
                        rust_tui_command(binary, options),
                        env=environment,
                        process_group=0,
                    )
                    if terminal is not None:
                        _set_foreground_pgrp(terminal.fd, process.pid)
                    status = _wait_for_frontend(process, supervisor_signals)
                    if supervisor_signals.signum is not None:
                        graceful_signal = supervisor_signals.signum
                        status = 128 + graceful_signal
            except KeyboardInterrupt:
                status = 128 + signal.SIGINT
                graceful_signal = signal.SIGINT
            except OSError as exc:
                action = "start Rust TUI binary" if process is None else "hand terminal to Rust TUI"
                raise RustTuiLaunchError(f"failed to {action}: {exc}") from exc
        finally:
            try:
                if process is not None:
                    _cleanup_process_group(process, graceful_signal=graceful_signal)
            except RustTuiLaunchError as exc:
                cleanup_error = exc
            finally:
                _restore_terminal(terminal)
    if supervisor_signals.signum is not None:
        status = 128 + supervisor_signals.signum
    if cleanup_error is not None:
        raise cleanup_error
    return status


def _snapshot_terminal() -> _TerminalSnapshot | None:
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return None
        return _TerminalSnapshot(
            fd=fd,
            foreground_pgrp=os.tcgetpgrp(fd),
            attributes=termios.tcgetattr(fd),
        )
    except (AttributeError, OSError, termios.error, ValueError):
        return None


def _restore_terminal(snapshot: _TerminalSnapshot | None) -> None:
    if snapshot is None:
        return
    try:
        _set_foreground_pgrp(snapshot.fd, snapshot.foreground_pgrp)
    except OSError:
        pass
    try:
        termios.tcsetattr(snapshot.fd, termios.TCSANOW, snapshot.attributes)
    except (OSError, termios.error):
        pass
    try:
        sys.stdout.write(_ANSI_BASELINE)
        sys.stdout.flush()
    except (AttributeError, OSError, ValueError):
        pass


def _set_foreground_pgrp(fd: int, pgrp: int) -> None:
    sigttou = getattr(signal, "SIGTTOU", None)
    if sigttou is None:
        os.tcsetpgrp(fd, pgrp)
        return
    previous = signal.signal(sigttou, signal.SIG_IGN)
    try:
        os.tcsetpgrp(fd, pgrp)
    finally:
        signal.signal(sigttou, previous)


@contextmanager
def _supervisor_signal_handlers() -> Iterator[_SupervisorSignals]:
    watched = tuple(
        signum
        for signum in (signal.SIGINT, getattr(signal, "SIGHUP", None), signal.SIGTERM)
        if signum is not None
    )
    previous: dict[int, signal._HANDLER] = {}
    observed = _SupervisorSignals()

    def interrupt(signum: int, _frame: FrameType | None) -> None:
        observed.signum = signum

    try:
        for signum in watched:
            previous[signum] = signal.signal(signum, interrupt)
        yield observed
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _wait_for_frontend(
    process: subprocess.Popen[bytes],
    observed: _SupervisorSignals,
) -> int:
    while True:
        status = process.poll()
        if status is not None:
            return status
        if observed.signum is not None:
            return 128 + observed.signum
        time.sleep(_POLL_SECONDS)


def _cleanup_process_group(
    process: subprocess.Popen[bytes],
    *,
    graceful_signal: int | None,
) -> None:
    pgid = process.pid
    if pgid == os.getpgrp():
        raise RustTuiLaunchError("refusing to clean up the launcher's own process group")
    if graceful_signal is not None and _process_group_exists(pgid):
        _signal_process_group(pgid, graceful_signal)
    if _wait_for_process_group(process, pgid, _GRACE_SECONDS):
        return
    _signal_process_group(pgid, signal.SIGTERM)
    if _wait_for_process_group(process, pgid, _TERM_SECONDS):
        return
    _signal_process_group(pgid, signal.SIGKILL)
    if not _wait_for_process_group(process, pgid, _KILL_SECONDS):
        raise RustTuiLaunchError("Rust TUI process group did not exit after SIGKILL")


def _wait_for_process_group(
    process: subprocess.Popen[bytes],
    pgid: int,
    timeout: float,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    deadline = clock() + timeout
    while True:
        process.poll()
        if not _process_group_exists(pgid):
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(_POLL_SECONDS, remaining))


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_process_group(pgid: int, signum: int) -> None:
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        pass
