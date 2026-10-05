from __future__ import annotations

import asyncio
import errno
import signal
import subprocess
import threading
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from wisp.tools.shell import process as process_tools_module


def test_kill_process_tree_and_wait_returns_failure_without_waiting(
    monkeypatch: MonkeyPatch,
) -> None:
    async def fail_terminate(_process: asyncio.subprocess.Process) -> bool:
        return False

    class DummyProcess:
        stdout = None
        stderr = None
        wait_called = False

        async def wait(self) -> int:
            self.wait_called = True
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    process = DummyProcess()
    monkeypatch.setattr(process_tools_module, "_terminate_process_tree", fail_terminate)

    async def run() -> bool:
        with anyio.fail_after(0.1):
            return await process_tools_module._kill_process_tree_and_wait(process)  # type: ignore[arg-type]  # noqa: SLF001

    assert anyio.run(run) is False
    assert process.wait_called is False


def test_posix_descendant_discovery_failure_reports_cleanup_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = None

    process = DummyProcess()
    setattr(process, process_tools_module._POSIX_JOBS_FILE_ATTR, tmp_path / "jobs")  # noqa: SLF001
    monkeypatch.setattr(process_tools_module.os, "name", "posix")
    monkeypatch.setattr(process_tools_module, "_posix_descendant_pids", lambda _pid: None)

    async def run() -> bool:
        return await process_tools_module._terminate_process_tree(  # type: ignore[arg-type]  # noqa: SLF001
            process,
            force=True,
        )

    assert anyio.run(run) is False


def test_posix_records_jobs_file_holder_pids(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = None

    jobs_file = tmp_path / "jobs"
    process = DummyProcess()
    setattr(process, process_tools_module._POSIX_JOBS_FILE_ATTR, jobs_file)  # noqa: SLF001
    monkeypatch.setattr(process_tools_module, "_posix_descendant_pids", lambda _pid: (234,))
    monkeypatch.setattr(process_tools_module, "_posix_jobs_file_holder_pids", lambda _path: (345,))

    recorded = process_tools_module._record_posix_descendant_pids(process)  # type: ignore[arg-type]  # noqa: SLF001

    assert recorded is True
    assert jobs_file.read_text(encoding="utf-8").splitlines() == ["234", "345"]


def test_posix_holder_discovery_failure_reports_cleanup_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = None

    jobs_file = tmp_path / "jobs"
    process = DummyProcess()
    setattr(process, process_tools_module._POSIX_JOBS_FILE_ATTR, jobs_file)  # noqa: SLF001
    monkeypatch.setattr(process_tools_module, "_posix_descendant_pids", lambda _pid: ())
    monkeypatch.setattr(process_tools_module, "_posix_jobs_file_holder_pids", lambda _path: None)

    assert process_tools_module._record_posix_descendant_pids(process) is False  # type: ignore[arg-type]  # noqa: SLF001


def test_posix_recorded_jobs_prune_stale_pids(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = None

    jobs_file = tmp_path / "jobs"
    jobs_file.write_text("234\n345\n456\n", encoding="utf-8")
    process = DummyProcess()
    setattr(process, process_tools_module._POSIX_JOBS_FILE_ATTR, jobs_file)  # noqa: SLF001
    monkeypatch.setattr(process_tools_module, "_posix_descendant_pids", lambda _pid: (234,))
    monkeypatch.setattr(process_tools_module, "_posix_jobs_file_holder_pids", lambda _path: (345,))
    monkeypatch.setattr(process_tools_module.os, "pidfd_open", None, raising=False)
    monkeypatch.setattr(process_tools_module.signal, "pidfd_send_signal", None, raising=False)
    kills: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(process_tools_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    signaled = process_tools_module._signal_posix_recorded_jobs(  # type: ignore[arg-type]  # noqa: SLF001
        process,
        signal.SIGKILL,
    )

    assert signaled is True
    assert kills == [(234, signal.SIGKILL), (345, signal.SIGKILL)]


def test_posix_recorded_jobs_skip_descendant_scan_after_leader_exit(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = 0

    jobs_file = tmp_path / "jobs"
    jobs_file.write_text("234\n345\n", encoding="utf-8")
    process = DummyProcess()
    setattr(process, process_tools_module._POSIX_JOBS_FILE_ATTR, jobs_file)  # noqa: SLF001

    def fail_descendant_scan(_pid: int) -> tuple[int, ...]:
        raise AssertionError("exited leader pid must not be traversed")

    monkeypatch.setattr(process_tools_module, "_posix_descendant_pids", fail_descendant_scan)
    monkeypatch.setattr(process_tools_module, "_posix_jobs_file_holder_pids", lambda _path: (345,))
    monkeypatch.setattr(process_tools_module.os, "pidfd_open", None, raising=False)
    monkeypatch.setattr(process_tools_module.signal, "pidfd_send_signal", None, raising=False)
    kills: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(process_tools_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    signaled = process_tools_module._signal_posix_recorded_jobs(  # type: ignore[arg-type]  # noqa: SLF001
        process,
        signal.SIGKILL,
    )

    assert signaled is True
    assert kills == [(345, signal.SIGKILL)]


def test_posix_recorded_jobs_revalidate_after_pidfd_open(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = None

    jobs_file = tmp_path / "jobs"
    jobs_file.write_text("234\n", encoding="utf-8")
    process = DummyProcess()
    setattr(process, process_tools_module._POSIX_JOBS_FILE_ATTR, jobs_file)  # noqa: SLF001
    events: list[str] = []
    closed_fds: list[int] = []

    def current_owned(_process: object) -> set[int]:
        events.append("snapshot")
        return set()

    def open_pidfd(_pid: int, _flags: int) -> int:
        events.append("open")
        return 789

    def fail_pidfd_send_signal(
        _pidfd: int,
        _selected_signal: signal.Signals,
        _siginfo: object,
        _flags: int,
    ) -> None:
        raise AssertionError("stale pidfd must not be signaled")

    monkeypatch.setattr(process_tools_module, "_posix_current_owned_pids", current_owned)
    monkeypatch.setattr(process_tools_module.os, "pidfd_open", open_pidfd, raising=False)
    monkeypatch.setattr(
        process_tools_module.signal,
        "pidfd_send_signal",
        fail_pidfd_send_signal,
        raising=False,
    )
    monkeypatch.setattr(process_tools_module, "_close_posix_fd", lambda fd: closed_fds.append(fd))

    signaled = process_tools_module._signal_posix_recorded_jobs(  # type: ignore[arg-type]  # noqa: SLF001
        process,
        signal.SIGKILL,
    )

    assert signaled is True
    assert events == ["open", "snapshot"]
    assert closed_fds == [789]


def test_posix_recorded_jobs_fall_back_when_pidfd_open_is_unsupported(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = None

    jobs_file = tmp_path / "jobs"
    jobs_file.write_text("234\n", encoding="utf-8")
    process = DummyProcess()
    setattr(process, process_tools_module._POSIX_JOBS_FILE_ATTR, jobs_file)  # noqa: SLF001
    kills: list[tuple[int, signal.Signals]] = []

    def unsupported_pidfd_open(_pid: int, _flags: int) -> int:
        raise OSError(errno.ENOSYS, "pidfd_open unavailable")

    def fail_pidfd_send_signal(*_args: object) -> None:
        raise AssertionError("pidfd_send_signal should not run after pidfd_open fails")

    monkeypatch.setattr(process_tools_module, "_posix_current_owned_pids", lambda _process: {234})
    monkeypatch.setattr(
        process_tools_module.os, "pidfd_open", unsupported_pidfd_open, raising=False
    )
    monkeypatch.setattr(
        process_tools_module.signal,
        "pidfd_send_signal",
        fail_pidfd_send_signal,
        raising=False,
    )
    monkeypatch.setattr(process_tools_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    signaled = process_tools_module._signal_posix_recorded_jobs(  # type: ignore[arg-type]  # noqa: SLF001
        process,
        signal.SIGKILL,
    )

    assert signaled is True
    assert kills == [(234, signal.SIGKILL)]


def test_posix_recorded_jobs_fall_back_when_pidfd_send_signal_is_unsupported(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = None

    jobs_file = tmp_path / "jobs"
    jobs_file.write_text("234\n", encoding="utf-8")
    process = DummyProcess()
    setattr(process, process_tools_module._POSIX_JOBS_FILE_ATTR, jobs_file)  # noqa: SLF001
    kills: list[tuple[int, signal.Signals]] = []
    closed_fds: list[int] = []

    def unsupported_pidfd_send_signal(
        _pidfd: int,
        _selected_signal: signal.Signals,
        _siginfo: object,
        _flags: int,
    ) -> None:
        raise OSError(errno.ENOSYS, "pidfd_send_signal unavailable")

    monkeypatch.setattr(process_tools_module, "_posix_current_owned_pids", lambda _process: {234})
    monkeypatch.setattr(
        process_tools_module.os, "pidfd_open", lambda _pid, _flags: 789, raising=False
    )
    monkeypatch.setattr(
        process_tools_module.signal,
        "pidfd_send_signal",
        unsupported_pidfd_send_signal,
        raising=False,
    )
    monkeypatch.setattr(process_tools_module, "_close_posix_fd", lambda fd: closed_fds.append(fd))
    monkeypatch.setattr(process_tools_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    signaled = process_tools_module._signal_posix_recorded_jobs(  # type: ignore[arg-type]  # noqa: SLF001
        process,
        signal.SIGKILL,
    )

    assert signaled is True
    assert kills == [(234, signal.SIGKILL)]
    assert closed_fds == [789]


def test_posix_recorded_jobs_batch_ownership_before_signaling(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = None

    jobs_file = tmp_path / "jobs"
    jobs_file.write_text("234\n345\n", encoding="utf-8")
    process = DummyProcess()
    setattr(process, process_tools_module._POSIX_JOBS_FILE_ATTR, jobs_file)  # noqa: SLF001
    kills: list[tuple[int, signal.Signals]] = []
    ownership_calls = 0

    def current_owned(_process: object) -> set[int]:
        nonlocal ownership_calls
        ownership_calls += 1
        return {234, 345}

    monkeypatch.setattr(process_tools_module, "_posix_current_owned_pids", current_owned)
    monkeypatch.setattr(process_tools_module.os, "pidfd_open", None, raising=False)
    monkeypatch.setattr(process_tools_module.signal, "pidfd_send_signal", None, raising=False)
    monkeypatch.setattr(process_tools_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    signaled = process_tools_module._signal_posix_recorded_jobs(  # type: ignore[arg-type]  # noqa: SLF001
        process,
        signal.SIGKILL,
    )

    assert signaled is True
    assert ownership_calls == 1
    assert kills == [(234, signal.SIGKILL), (345, signal.SIGKILL)]


def test_open_posix_jobs_fd_uses_descriptor_below_soft_limit(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    import fcntl
    import resource

    jobs_file = tmp_path / "jobs"
    jobs_file.write_text("", encoding="utf-8")
    closed_fds: list[int] = []
    minimum_fds: list[int] = []

    def fake_fcntl(_fd: int, command: int, minimum_fd: int) -> int:
        assert command == fcntl.F_DUPFD
        minimum_fds.append(minimum_fd)
        if minimum_fd >= 64:
            raise OSError(errno.EINVAL, "minimum fd is above soft limit")
        return 63

    monkeypatch.setattr(process_tools_module.os, "open", lambda _path, _flags: 3)
    monkeypatch.setattr(process_tools_module, "_close_posix_fd", lambda fd: closed_fds.append(fd))
    monkeypatch.setattr(resource, "getrlimit", lambda _limit: (64, 64))
    monkeypatch.setattr(fcntl, "fcntl", fake_fcntl)

    jobs_fd = process_tools_module._open_posix_jobs_fd(jobs_file)  # noqa: SLF001

    assert jobs_fd == 63
    assert minimum_fds == [63]
    assert closed_fds == [3]


def test_posix_recorded_job_verification_runs_off_event_loop(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = None

    worker_thread_ids: list[int] = []

    def fake_record(_process: object) -> bool:
        return True

    def fake_signal_group(_process: object, _selected_signal: signal.Signals) -> bool:
        return True

    def fake_signal_recorded(_process: object, _selected_signal: signal.Signals) -> bool:
        worker_thread_ids.append(threading.get_ident())
        return True

    monkeypatch.setattr(process_tools_module.os, "name", "posix")
    monkeypatch.setattr(process_tools_module, "_record_posix_descendant_pids", fake_record)
    monkeypatch.setattr(process_tools_module, "_signal_posix_process_group", fake_signal_group)
    monkeypatch.setattr(process_tools_module, "_signal_posix_recorded_jobs", fake_signal_recorded)

    async def run() -> int:
        event_loop_thread_id = threading.get_ident()
        assert await process_tools_module._terminate_process_tree(DummyProcess()) is True  # type: ignore[arg-type]  # noqa: SLF001
        return event_loop_thread_id

    event_loop_thread_id = anyio.run(run)

    assert worker_thread_ids
    assert all(thread_id != event_loop_thread_id for thread_id in worker_thread_ids)


def test_posix_jobs_file_holder_pids_uses_lsof(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    jobs_file = tmp_path / "jobs"
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="234\nnot-a-pid\n345\n")

    monkeypatch.setattr(
        process_tools_module, "_posix_jobs_file_holder_pids_from_proc", lambda _path: None
    )
    monkeypatch.setattr(process_tools_module.subprocess, "run", fake_run)

    assert process_tools_module._posix_jobs_file_holder_pids(jobs_file) == (234, 345)  # noqa: SLF001
    assert calls == [["lsof", "-t", "--", str(jobs_file)]]


def test_posix_jobs_file_holder_pids_uses_proc_before_external_tools(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    jobs_file = tmp_path / "jobs"

    def fail_run(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("external holder probe should not run")

    monkeypatch.setattr(
        process_tools_module, "_posix_jobs_file_holder_pids_from_proc", lambda _path: (234,)
    )
    monkeypatch.setattr(process_tools_module.subprocess, "run", fail_run)

    assert process_tools_module._posix_jobs_file_holder_pids(jobs_file) == (234,)  # noqa: SLF001


def test_posix_jobs_file_holder_pids_falls_back_to_fuser(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    jobs_file = tmp_path / "jobs"
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[0] == "lsof":
            raise OSError
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{jobs_file}: 234 345\n",
            stderr="",
        )

    monkeypatch.setattr(
        process_tools_module, "_posix_jobs_file_holder_pids_from_proc", lambda _path: None
    )
    monkeypatch.setattr(process_tools_module.subprocess, "run", fake_run)

    assert process_tools_module._posix_jobs_file_holder_pids(jobs_file) == (234, 345)  # noqa: SLF001
    assert calls == [["lsof", "-t", "--", str(jobs_file)], ["fuser", str(jobs_file)]]


def test_posix_jobs_file_holder_pids_reports_probe_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    jobs_file = tmp_path / "jobs"

    def fake_run(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError

    monkeypatch.setattr(
        process_tools_module, "_posix_jobs_file_holder_pids_from_proc", lambda _path: None
    )
    monkeypatch.setattr(process_tools_module.subprocess, "run", fake_run)

    assert process_tools_module._posix_jobs_file_holder_pids(jobs_file) is None  # noqa: SLF001


def test_posix_permission_error_reports_cleanup_failure_for_running_leader(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = None
        killed = False

        def kill(self) -> None:
            self.killed = True

    def fail_killpg(_pid: int, _signal: int) -> None:
        raise PermissionError

    process = DummyProcess()
    monkeypatch.setattr(process_tools_module.os, "name", "posix")
    monkeypatch.setattr(process_tools_module.os, "killpg", fail_killpg)

    assert process_tools_module._kill_process_tree(process) is False  # type: ignore[arg-type]  # noqa: SLF001
    assert process.killed is True


@pytest.mark.process
def test_posix_group_signal_skips_exited_leader_pid(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = 0

    def fail_killpg(_pid: int, _signal: int) -> None:
        raise AssertionError("exited leader pid must not be signaled as a process group")

    monkeypatch.setattr(process_tools_module.os, "killpg", fail_killpg)

    assert (
        process_tools_module._signal_posix_process_group(  # type: ignore[arg-type]  # noqa: SLF001
            DummyProcess(),
            signal.SIGKILL,
        )
        is True
    )


def test_posix_shell_uses_hidden_high_jobs_fd(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    class DummyProcess:
        returncode = 0

    creation_calls: list[dict[str, object]] = []
    closed_fds: list[int] = []
    process = DummyProcess()

    async def fake_create_subprocess_shell(
        command: str,
        **kwargs: object,
    ) -> DummyProcess:
        assert "exec 9" not in command
        creation_calls.append(kwargs)
        return process

    monkeypatch.setattr(process_tools_module.os, "name", "posix")
    monkeypatch.setattr(process_tools_module.sys, "platform", "darwin")
    monkeypatch.setattr(process_tools_module, "_open_posix_jobs_fd", lambda _path: 123)
    monkeypatch.setattr(process_tools_module, "_close_posix_fd", lambda fd: closed_fds.append(fd))
    monkeypatch.setattr(
        process_tools_module.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )

    async def run() -> DummyProcess:
        return await process_tools_module._create_shell_process("echo hi", cwd=tmp_path)  # type: ignore[return-value]  # noqa: SLF001

    result = anyio.run(run)

    assert result is process
    assert creation_calls[0]["pass_fds"] == (123,)
    assert creation_calls[0]["start_new_session"] is True
    assert closed_fds == [123]
    process_tools_module._remove_posix_jobs_file(  # noqa: SLF001
        getattr(process, process_tools_module._POSIX_JOBS_FILE_ATTR),  # noqa: SLF001
    )


def test_linux_posix_shell_startup_uses_exec_helper_instead_of_preexec(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    class DummyProcess:
        pass

    process = DummyProcess()
    exec_calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    closed_fds: list[int] = []

    async def create_exec(*command: str, **kwargs: object) -> DummyProcess:
        exec_calls.append((command, kwargs))
        return process

    async def fail_shell(*_args: object, **_kwargs: object) -> DummyProcess:
        raise AssertionError("Linux POSIX startup should use exec helper")

    monkeypatch.setattr(process_tools_module.os, "name", "posix")
    monkeypatch.setattr(process_tools_module.sys, "platform", "linux")
    monkeypatch.setattr(process_tools_module, "_open_posix_jobs_fd", lambda _path: 123)
    monkeypatch.setattr(process_tools_module, "_close_posix_fd", lambda fd: closed_fds.append(fd))
    monkeypatch.setattr(process_tools_module.asyncio, "create_subprocess_exec", create_exec)
    monkeypatch.setattr(process_tools_module.asyncio, "create_subprocess_shell", fail_shell)

    async def run() -> DummyProcess:
        return await process_tools_module._create_shell_process("echo hi", cwd=tmp_path)  # type: ignore[return-value]  # noqa: SLF001

    result = anyio.run(run)
    jobs_file = getattr(result, process_tools_module._POSIX_JOBS_FILE_ATTR)  # noqa: SLF001
    jobs_file.unlink(missing_ok=True)

    assert result is process
    assert len(exec_calls) == 1
    command, kwargs = exec_calls[0]
    assert command[:3] == (
        process_tools_module.sys.executable,
        "-c",
        process_tools_module._POSIX_SUBREAPER_HELPER,  # noqa: SLF001
    )
    assert "echo hi" in command[3]
    assert "preexec_fn" not in kwargs
    assert kwargs["pass_fds"] == (123,)
    assert kwargs["start_new_session"] is True
    assert closed_fds == [123]


def test_posix_shell_spawn_cancellation_cleans_tracking_resources(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    closed_fds: list[int] = []
    removed_files: list[object] = []

    async def cancelled_create_subprocess_exec(
        *_command: str,
        **_kwargs: object,
    ) -> object:
        raise asyncio.CancelledError

    async def fail_create_subprocess_shell(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Linux POSIX startup should use exec helper")

    monkeypatch.setattr(process_tools_module.os, "name", "posix")
    monkeypatch.setattr(process_tools_module.sys, "platform", "linux")
    monkeypatch.setattr(process_tools_module, "_open_posix_jobs_fd", lambda _path: 123)
    monkeypatch.setattr(process_tools_module, "_close_posix_fd", lambda fd: closed_fds.append(fd))
    monkeypatch.setattr(
        process_tools_module, "_remove_posix_jobs_file", lambda path: removed_files.append(path)
    )
    monkeypatch.setattr(
        process_tools_module.asyncio,
        "create_subprocess_exec",
        cancelled_create_subprocess_exec,
    )
    monkeypatch.setattr(
        process_tools_module.asyncio,
        "create_subprocess_shell",
        fail_create_subprocess_shell,
    )

    async def run() -> None:
        await process_tools_module._create_shell_process("echo hi", cwd=tmp_path)  # noqa: SLF001

    with pytest.raises(asyncio.CancelledError):
        anyio.run(run)

    assert closed_fds == [123]
    assert len(removed_files) == 1
