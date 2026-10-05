from __future__ import annotations

import os
import shlex
import sys
import time
from pathlib import Path

import anyio
import pytest

from tests.tools.shell.supervisor_support import (
    poll_until_terminal,
    wait_for_pid_file,
)
from wisp.tools.result import ToolError
from wisp.tools.shell.supervisor import (
    ProcessSupervisor,
    ProcessUpdate,
)

pytestmark = pytest.mark.process


def _detached_python_background_command(
    pid_path: Path,
    *,
    keep_shell_alive: bool,
    redirect_output: bool,
) -> str:
    child = (
        "import os,pathlib,time;"
        "os.setsid();"
        f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()));"
        "time.sleep(30)"
    )
    redirection = " >/dev/null 2>&1" if redirect_output else ""
    quoted_pid_path = shlex.quote(str(pid_path))
    command = (
        f"{shlex.quote(sys.executable)} -c {shlex.quote(child)}{redirection} & "
        f"while [ ! -s {quoted_pid_path} ]; do sleep 0.01; done"
    )
    if keep_shell_alive:
        command += "; sleep 30"
    return command


def _nested_detached_python_background_command(pid_path: Path) -> str:
    child = (
        "import os,pathlib,time;"
        "os.setsid();"
        f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()));"
        "time.sleep(30)"
    )
    nested = f"{shlex.quote(sys.executable)} -c {shlex.quote(child)} >/dev/null 2>&1 &"
    quoted_pid_path = shlex.quote(str(pid_path))
    return f"sh -c {shlex.quote(nested)}; while [ ! -s {quoted_pid_path} ]; do sleep 0.01; done"


def _assert_process_gone(pid: int) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    pytest.fail(f"process {pid} remained alive")


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group assertion")
def test_timeout_kills_descendant_after_shell_leader_exits(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "background.pid"
    command = f"sleep 30 & echo $! > {shlex.quote(str(child_pid_path))}"

    async def run() -> tuple[ProcessUpdate, int]:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(
                command,
                cwd=tmp_path,
                timeout=0.1,
            )
            child_pid = await wait_for_pid_file(child_pid_path)
            terminal = (await poll_until_terminal(supervisor, process_id))[-1]
            return terminal, child_pid
        finally:
            await supervisor.aclose()

    update, child_pid = anyio.run(run)

    assert update.state == "timed_out"
    assert update.exit_code is None
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group assertion")
def test_timeout_kills_detached_background_job(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "detached.pid"

    async def run() -> tuple[ProcessUpdate, int]:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(
                _detached_python_background_command(
                    child_pid_path,
                    keep_shell_alive=True,
                    redirect_output=False,
                ),
                cwd=tmp_path,
                timeout=1,
            )
            child_pid = await wait_for_pid_file(child_pid_path)
            terminal = (await poll_until_terminal(supervisor, process_id))[-1]
            return terminal, child_pid
        finally:
            await supervisor.aclose()

    update, child_pid = anyio.run(run)

    assert update.state == "timed_out"
    _assert_process_gone(child_pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group assertion")
def test_close_kills_descendant_after_shell_leader_exits(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "background.pid"
    command = f"sleep 30 & echo $! > {shlex.quote(str(child_pid_path))}"

    async def run() -> int:
        supervisor = ProcessSupervisor()
        await supervisor.start(
            command,
            cwd=tmp_path,
            timeout=30,
        )
        child_pid = await wait_for_pid_file(child_pid_path)
        await anyio.sleep(0.05)
        await supervisor.aclose()
        return child_pid

    child_pid = anyio.run(run)

    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group assertion")
def test_completion_kills_descendant_with_redirected_output(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "background.pid"
    command = f"sleep 30 >/dev/null 2>&1 & echo $! > {shlex.quote(str(child_pid_path))}"

    async def run() -> ProcessUpdate:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(
                command,
                cwd=tmp_path,
                timeout=2,
            )
            child_pid = await wait_for_pid_file(child_pid_path)
            terminal = (await poll_until_terminal(supervisor, process_id))[-1]
            with anyio.fail_after(3):
                while True:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        break
                    await anyio.sleep(0.01)
            return terminal
        finally:
            await supervisor.aclose()

    update = anyio.run(run)

    assert update.state == "completed"
    assert update.exit_code == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group assertion")
def test_completion_kills_detached_background_job(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "detached.pid"

    async def run() -> tuple[ProcessUpdate, int]:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(
                _detached_python_background_command(
                    child_pid_path,
                    keep_shell_alive=False,
                    redirect_output=True,
                ),
                cwd=tmp_path,
                timeout=2,
            )
            child_pid = await wait_for_pid_file(child_pid_path)
            terminal = (await poll_until_terminal(supervisor, process_id))[-1]
            return terminal, child_pid
        finally:
            await supervisor.aclose()

    update, child_pid = anyio.run(run)

    assert update.state == "completed"
    assert update.exit_code == 0
    _assert_process_gone(child_pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group assertion")
def test_exit_command_records_and_kills_detached_background_job(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "detached-before-exit.pid"
    command = (
        _detached_python_background_command(
            child_pid_path,
            keep_shell_alive=False,
            redirect_output=True,
        )
        + "; exit 0"
    )

    async def run() -> tuple[ProcessUpdate, int]:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(command, cwd=tmp_path, timeout=2)
            child_pid = await wait_for_pid_file(child_pid_path)
            terminal = (await poll_until_terminal(supervisor, process_id))[-1]
            return terminal, child_pid
        finally:
            await supervisor.aclose()

    update, child_pid = anyio.run(run)

    assert update.state == "completed"
    assert update.exit_code == 0
    _assert_process_gone(child_pid)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux child-subreaper assertion",
)
def test_exec_command_does_not_bypass_detached_background_job_recording(
    tmp_path: Path,
) -> None:
    child_pid_path = tmp_path / "detached-before-exec.pid"
    command = (
        _detached_python_background_command(
            child_pid_path,
            keep_shell_alive=False,
            redirect_output=True,
        )
        + "; exec true"
    )

    async def run() -> tuple[ProcessUpdate, int]:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(command, cwd=tmp_path, timeout=2)
            child_pid = await wait_for_pid_file(child_pid_path)
            terminal = (await poll_until_terminal(supervisor, process_id))[-1]
            return terminal, child_pid
        finally:
            await supervisor.aclose()

    update, child_pid = anyio.run(run)

    assert update.state == "completed"
    assert update.exit_code == 0
    _assert_process_gone(child_pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group assertion")
def test_endless_output_times_out_and_kills_detached_background_job(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "detached-output-limit.pid"
    command = (
        _detached_python_background_command(
            child_pid_path,
            keep_shell_alive=False,
            redirect_output=True,
        )
        + "; yes x"
    )

    async def run() -> int:
        supervisor = ProcessSupervisor()
        try:
            with pytest.raises(ToolError, match="timed out"):
                await supervisor.run_to_completion(
                    command,
                    cwd=tmp_path,
                    timeout=2,
                    max_output_bytes=20,
                    max_output_lines=100,
                )
            return await wait_for_pid_file(child_pid_path)
        finally:
            await supervisor.aclose()

    child_pid = anyio.run(run)

    _assert_process_gone(child_pid)


@pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX detached process assertion",
)
def test_completion_kills_nested_shell_detached_background_job(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "nested-detached.pid"

    async def run() -> tuple[ProcessUpdate, int]:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(
                _nested_detached_python_background_command(child_pid_path),
                cwd=tmp_path,
                timeout=2,
            )
            child_pid = await wait_for_pid_file(child_pid_path)
            terminal = (await poll_until_terminal(supervisor, process_id))[-1]
            return terminal, child_pid
        finally:
            await supervisor.aclose()

    update, child_pid = anyio.run(run)

    assert update.state == "completed"
    assert update.exit_code == 0
    _assert_process_gone(child_pid)
