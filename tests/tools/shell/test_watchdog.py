"""Commands stop when the backend that started them is killed outright."""

from __future__ import annotations

import errno
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import anyio
import pytest
from pytest import LogCaptureFixture, MonkeyPatch

from wisp.tools.shell import watchdog as watchdog_module
from wisp.tools.shell.process import ProcessResult
from wisp.tools.shell.supervisor import ProcessSupervisor

pytestmark = [
    pytest.mark.process,
    pytest.mark.skipif(os.name != "posix", reason="POSIX process cleanup"),
]

# A daemon that leaves the session and closes every descriptor it inherited, so only
# its command token can find it. It is Python rather than a system program because
# macOS hides the environment of protected system binaries.
_DAEMON = """
import os, pathlib, sys, time
if os.fork():
    os._exit(0)
os.setsid()
if os.fork():
    os._exit(0)
null = os.open(os.devnull, os.O_RDWR)
for fd in (0, 1, 2):
    os.dup2(null, fd)
os.closerange(3, 1024)
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(60)
"""

# Stands in for a backend: it starts one managed process that leaves a daemon behind,
# then either waits to be killed or shuts down cleanly when asked to.
_BACKEND = """
import shlex, sys
from pathlib import Path

import anyio

from wisp.tools.shell import watchdog
from wisp.tools.shell.supervisor import ProcessSupervisor

pid_path, daemon_path, daemon_script, watchdog_path, mode = sys.argv[1:6]
daemon = f"{shlex.quote(sys.executable)} {shlex.quote(daemon_script)} {shlex.quote(daemon_path)}"


async def main() -> None:
    supervisor = ProcessSupervisor()
    await supervisor.start(
        f"echo $$ > {shlex.quote(pid_path)}; {daemon}; exec sleep 60",
        cwd=Path.cwd(),
        timeout=60,
    )
    Path(watchdog_path).write_text(str(watchdog._WATCHDOG.pid()))
    if mode == "close":
        while not Path(daemon_path).exists():
            await anyio.sleep(0.02)
        await supervisor.aclose()
        return
    await anyio.sleep(60)


anyio.run(main)
"""


def _wait_for_pid(path: Path, backend: subprocess.Popen[bytes]) -> int:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        text = path.read_text().strip() if path.exists() else ""
        if text:
            return int(text)
        if backend.poll() is not None:
            pytest.fail(f"backend exited with {backend.returncode} before writing {path.name}")
        time.sleep(0.02)
    pytest.fail(f"backend did not write {path.name}")


def _gone(pid: int, *, group: bool = False, timeout: float = 20) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if group:
                os.killpg(pid, 0)
            else:
                os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _start_backend(tmp_path: Path, mode: str) -> tuple[subprocess.Popen[bytes], dict[str, Path]]:
    paths = {name: tmp_path / name for name in ("managed", "daemon", "watchdog")}
    (tmp_path / "backend.py").write_text(_BACKEND, encoding="utf-8")
    (tmp_path / "daemon.py").write_text(_DAEMON, encoding="utf-8")
    backend = subprocess.Popen(
        [
            sys.executable,
            str(tmp_path / "backend.py"),
            str(paths["managed"]),
            str(paths["daemon"]),
            str(tmp_path / "daemon.py"),
            str(paths["watchdog"]),
            mode,
        ],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return backend, paths


def _kill_leftovers(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_killed_backend_stops_commands(tmp_path: Path) -> None:
    backend, paths = _start_backend(tmp_path, "wait")
    pids: list[int] = []
    try:
        pids = [_wait_for_pid(paths[name], backend) for name in ("managed", "daemon", "watchdog")]

        backend.kill()
        backend.wait(timeout=10)

        managed, daemon, watchdog = pids
        # The shell leads its process group, which also holds its job-recorder subshell.
        assert _gone(managed, group=True), "managed process group outlived the backend"
        assert _gone(daemon), "detached daemon outlived the backend"
        assert _gone(watchdog), "watchdog did not exit"
    finally:
        if backend.poll() is None:
            backend.kill()
            backend.wait()
        _kill_leftovers(pids)


def test_watchdog_exits_after_clean_shutdown(tmp_path: Path) -> None:
    backend, paths = _start_backend(tmp_path, "close")
    pids: list[int] = []
    try:
        pids = [_wait_for_pid(paths[name], backend) for name in ("managed", "daemon", "watchdog")]

        assert backend.wait(timeout=30) == 0

        assert _gone(pids[2]), "watchdog outlived a clean shutdown"
    finally:
        if backend.poll() is None:
            backend.kill()
            backend.wait()
        _kill_leftovers(pids)


def test_commands_run_without_watchdog(
    tmp_path: Path, monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    def cannot_start() -> subprocess.Popen[bytes]:
        raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))

    monkeypatch.setattr(watchdog_module, "_WATCHDOG", watchdog_module._Watchdog())
    monkeypatch.setattr(watchdog_module, "_start_watchdog_process", cannot_start)

    async def run_twice() -> list[ProcessResult]:
        supervisor = ProcessSupervisor()
        try:
            return [
                await supervisor.run_to_completion(
                    "echo hi", cwd=tmp_path, timeout=10, max_output_bytes=1_000
                )
                for _ in range(2)
            ]
        finally:
            await supervisor.aclose()

    with caplog.at_level(logging.WARNING, logger=watchdog_module.__name__):
        results = anyio.run(run_twice)

    assert [result.stdout for result in results] == ["hi\n", "hi\n"]
    assert [record.getMessage() for record in caplog.records] == [
        "The process watchdog could not start: [Errno 24] Too many open files. Commands "
        "still run, but a backend crash would leave them running."
    ]


def test_ignores_partial_last_line() -> None:
    watched = watchdog_module.WatchedCommand(
        pid=11, token="t", started_at=0.0, jobs_file="/tmp/jobs", leader_start=None
    )
    released = watchdog_module.WatchedCommand(
        pid=12, token="u", started_at=0.0, jobs_file="/tmp/jobs-2", leader_start=None
    )
    lines = [
        *watchdog_module._Watchdog._watch_lines([watched, released]),
        b'{"release": 12}\n',
        b'{"watch": {"pid": 13',
    ]

    assert watchdog_module._read_watched_commands(lines) == [watched]
