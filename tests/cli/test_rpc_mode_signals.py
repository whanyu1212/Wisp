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
# managed process, standing in for one the model started with operation=start.
_BACKEND = """
import sys
from functools import partial
from pathlib import Path

import anyio

from wisp.cli import rpc as cli_rpc
from wisp.config.runtime import WispConfig

pid_path = Path(sys.argv[1])
build_runtime = cli_rpc.build_session_runtime


async def build_with_managed_process(config):
    runtime = await build_runtime(config)
    await runtime.process_supervisor.start(
        f"echo $$ > {pid_path}; exec sleep 60", cwd=Path.cwd(), timeout=60
    )
    return runtime


cli_rpc.build_session_runtime = build_with_managed_process
anyio.run(partial(cli_rpc._run_rpc, WispConfig(), handshake_complete=True))
"""


def _wait_for_pid(path: Path, backend: subprocess.Popen[bytes]) -> int:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if backend.poll() is not None:
            pytest.fail(f"backend exited with {backend.returncode} before starting")
        text = path.read_text().strip() if path.exists() else ""
        if text:
            return int(text)
        time.sleep(0.02)
    pytest.fail("backend did not start its managed process")


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
    backend = subprocess.Popen(
        [sys.executable, str(script), str(pid_path)],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    managed_pid: int | None = None
    try:
        managed_pid = _wait_for_pid(pid_path, backend)

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
