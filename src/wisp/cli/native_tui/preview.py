"""Supervise the optional, single-request history preview worker."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from threading import Thread
from typing import TYPE_CHECKING

import wisp

if TYPE_CHECKING:
    from .launch import TuiOptions


_warmed: ContextVar[subprocess.Popen[bytes] | None] = ContextVar("startup_worker", default=None)


def _stdio_is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


# A wedged backend may never start draining the pipe. Reap the worker even if
# the TUI itself stays alive indefinitely.
WORKER_LIFETIME_SECONDS = 10.0


@contextmanager
def prewarm_preview(options: TuiOptions) -> Iterator[subprocess.Popen[bytes] | None]:
    """Start importing alongside preflight, and reap on every launcher exit."""

    eligible = options.project_trusted and (options.resume is not None or options.continue_latest)
    if not eligible or not _stdio_is_interactive():
        # A speculative import must not leave a worker waiting throughout an
        # untrusted session. No session path has been sent.
        if (unused := _warmed.get()) is not None:
            stop_worker(unused)
        yield None
    elif (worker := _warmed.get()) is not None:
        yield worker
    else:
        with _worker_process() as worker:
            yield worker


@contextmanager
def warm_cli_imports() -> Iterator[None]:
    """Overlap package imports only; the worker receives no path until trusted preflight."""
    with _worker_process() as worker:
        token = _warmed.set(worker)
        try:
            yield
        finally:
            _warmed.reset(token)


@contextmanager
def _worker_process() -> Iterator[subprocess.Popen[bytes] | None]:
    process = None
    if _stdio_is_interactive():
        with suppress(OSError):
            # Import exactly the launcher's package, outside the project cwd. The
            # worker needs neither the project environment nor provider credentials.
            process = subprocess.Popen(
                [sys.executable, "-B", "-m", "wisp.sessions.startup_preview"],
                cwd=Path(wisp.__file__).resolve().parent.parent,
                env={},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
    try:
        yield process
    finally:
        if process is not None:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.close()
            process.stdout.close()
            stop_worker(process)


def stop_worker(process: subprocess.Popen[bytes]) -> None:
    """Bound cleanup even when a worker is stuck and ignores graceful shutdown."""

    process.kill()
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        # A slow kernel exit must not hold up the terminal or leave a zombie.
        Thread(target=process.wait, name="wisp-preview-reaper", daemon=True).start()


def request_preview(process: subprocess.Popen[bytes], path: Path, cwd: Path) -> int | None:
    """Send the validated request without waiting for the worker to read its stdin."""

    assert process.stdin is not None and process.stdout is not None
    try:
        request = json.dumps([str(path.absolute()), str(cwd)]).encode("ascii")
        fd = process.stdin.fileno()
        os.set_blocking(fd, False)
        if os.write(fd, request) == len(request) and process.stdout.fileno() >= 3:
            return process.stdout.fileno()
    except OSError:
        pass
    finally:
        process.stdin.close()
    return None
