"""The RPC backend stops its managed processes when it is told to shut down."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Skip before the parameters below name SIGHUP, which Windows does not define.
if os.name != "posix":
    pytest.skip("POSIX shutdown signals", allow_module_level=True)

pytestmark = pytest.mark.process

# Runs the real backend entry point with a runtime that has already started one
# managed process, standing in for one the model started with operation=start. The
# process starts while the runtime is built, before the backend handles signals, so
# the backend also writes a ready file once its signal receiver is installed.
_BACKEND = """
import sys
from contextlib import contextmanager
from functools import partial
from pathlib import Path

import anyio

from wisp.cli import rpc as cli_rpc
from wisp.config.runtime import WispConfig

pid_path = Path(sys.argv[1])
ready_path = Path(sys.argv[2])
build_runtime = cli_rpc.build_session_runtime
signal_receiver = cli_rpc._shutdown_signal_receiver


async def build_with_managed_process(config):
    runtime = await build_runtime(config)
    await runtime.process_supervisor.start(
        f"echo $$ > {pid_path}; exec sleep 60", cwd=Path.cwd(), timeout=60
    )
    return runtime


@contextmanager
def announced_signal_receiver():
    with signal_receiver() as signals:
        ready_path.write_text("ready")
        yield signals


cli_rpc.build_session_runtime = build_with_managed_process
cli_rpc._shutdown_signal_receiver = announced_signal_receiver
anyio.run(partial(cli_rpc._run_rpc, WispConfig(), handshake_complete=True))
"""


def _wait_for_text(path: Path, backend: subprocess.Popen[bytes]) -> str:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if backend.poll() is not None:
            pytest.fail(f"backend exited with {backend.returncode} before starting")
        text = path.read_text().strip() if path.exists() else ""
        if text:
            return text
        time.sleep(0.02)
    pytest.fail(f"backend did not write {path.name}")


def _process_gone(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


@pytest.mark.parametrize("shutdown_signal", [signal.SIGTERM, signal.SIGHUP], ids=["term", "hup"])
def test_signal_stops_managed_processes(tmp_path: Path, shutdown_signal: signal.Signals) -> None:
    script = tmp_path / "backend.py"
    script.write_text(_BACKEND, encoding="utf-8")
    pid_path = tmp_path / "managed.pid"
    ready_path = tmp_path / "ready"
    backend = subprocess.Popen(
        [sys.executable, str(script), str(pid_path), str(ready_path)],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    managed_pid: int | None = None
    try:
        managed_pid = int(_wait_for_text(pid_path, backend))
        _wait_for_text(ready_path, backend)

        backend.send_signal(shutdown_signal)

        assert backend.wait(timeout=20) == 0
        assert _process_gone(managed_pid)
    finally:
        if backend.poll() is None:
            backend.kill()
            backend.wait()
        if backend.stdin is not None:
            backend.stdin.close()
        if managed_pid is not None:
            try:
                os.kill(managed_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
