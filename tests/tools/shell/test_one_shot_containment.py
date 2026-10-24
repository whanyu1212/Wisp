"""Adversarial containment checks for one-shot commands that exit cleanly.

Each command leaves a process behind that tries to outlive the shell in a different
way, then exits with status 0. The one-shot cleanup must kill every survivor before
``run_to_completion`` returns, whichever discovery path (process group, recorded job,
or jobs-file holder) is the only one able to find it.
"""

from __future__ import annotations

import os
import shlex
import sys
import time
from pathlib import Path

import anyio
import pytest

from wisp.tools.shell.process import ProcessResult
from wisp.tools.shell.supervisor import ProcessSupervisor

pytestmark = [
    pytest.mark.process,
    pytest.mark.skipif(os.name != "posix", reason="POSIX process-tree containment"),
]


def _python(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def _write_own_pid(pid_path: Path) -> str:
    return f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()))\n"


def _wait_for_pid_file(pid_path: Path) -> str:
    return f"while [ ! -s {shlex.quote(str(pid_path))} ]; do sleep 0.01; done"


def _setsid_daemon(pid_path: Path) -> str:
    """A background job that starts a new session but keeps the inherited fds."""

    source = f"import os, pathlib, time\nos.setsid()\n{_write_own_pid(pid_path)}time.sleep(30)\n"
    return f"{_python(source)} >/dev/null 2>&1 & {_wait_for_pid_file(pid_path)}"


def _double_fork_daemon(pid_path: Path) -> str:
    """A classic daemon: fork, setsid, fork again, and let both parents exit.

    The survivor is reparented away from the shell before the job recorder can see it
    and leaves the process group, so only the jobs-file holder scan can find it.
    """

    source = (
        "import os, pathlib, time\n"
        "if os.fork():\n"
        "    os._exit(0)\n"
        "os.setsid()\n"
        "if os.fork():\n"
        "    os._exit(0)\n"
        f"{_write_own_pid(pid_path)}"
        "time.sleep(30)\n"
    )
    return f"{_python(source)} >/dev/null 2>&1; {_wait_for_pid_file(pid_path)}"


def _process_group_escape(pid_path: Path) -> str:
    """A background job that moves into its own process group within the session."""

    source = (
        f"import os, pathlib, time\nos.setpgid(0, 0)\n{_write_own_pid(pid_path)}time.sleep(30)\n"
    )
    return f"{_python(source)} >/dev/null 2>&1 & {_wait_for_pid_file(pid_path)}"


def _nohup_job(pid_path: Path) -> str:
    """A ``nohup`` background job that ignores SIGHUP."""

    return f"nohup sleep 30 >/dev/null 2>&1 & echo $! > {shlex.quote(str(pid_path))}"


def _sigterm_ignoring_job(pid_path: Path) -> str:
    """A background job that survives the SIGTERM pass and needs the SIGKILL pass."""

    return f"(trap '' TERM; exec sleep 30) >/dev/null 2>&1 & echo $! > {shlex.quote(str(pid_path))}"


def _orphaned_group_member_without_tracking_fds(pid_path: Path) -> str:
    """An orphaned job that closed every inherited descriptor above stderr.

    It holds neither the jobs file nor any other tracking descriptor, so only the
    process-group signal can find it.
    """

    source = (
        "import os, pathlib, time\n"
        "os.closerange(3, 1024)\n"
        f"{_write_own_pid(pid_path)}"
        "time.sleep(30)\n"
    )
    return f"({_python(source)} >/dev/null 2>&1 &); {_wait_for_pid_file(pid_path)}"


def _assert_process_gone(pid: int) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        return
    pytest.fail(f"process {pid} outlived a cleanly exiting one-shot command")


@pytest.mark.parametrize(
    "build_command",
    [
        _setsid_daemon,
        _double_fork_daemon,
        _process_group_escape,
        _nohup_job,
        _sigterm_ignoring_job,
        _orphaned_group_member_without_tracking_fds,
    ],
    ids=lambda build_command: build_command.__name__.lstrip("_"),
)
def test_clean_one_shot_exit_kills_escaped_process(
    tmp_path: Path,
    build_command: object,
) -> None:
    assert callable(build_command)
    pid_path = tmp_path / "survivor.pid"
    command = build_command(pid_path)

    async def run() -> ProcessResult:
        supervisor = ProcessSupervisor()
        try:
            return await supervisor.run_to_completion(
                command,
                cwd=tmp_path,
                timeout=10,
                max_output_bytes=10_000,
            )
        finally:
            await supervisor.aclose()

    result = anyio.run(run)
    survivor_pid = int(pid_path.read_text())

    assert result.exit_code == 0, result
    _assert_process_gone(survivor_pid)
