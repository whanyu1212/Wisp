from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from wisp.tools.result import ToolError
from wisp.tools.shell import process as process_tools_module


def test_bash_tool_uses_taskkill_for_windows_process_tree_cleanup(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        returncode = None
        pid = 123
        _handle = 456
        killed = False

        def kill(self) -> None:
            self.killed = True

    class FakeKernel32:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def WaitForSingleObject(self, process: int, milliseconds: int) -> int:
            self.calls.append(("wait", process, milliseconds))
            return process_tools_module._WINDOWS_WAIT_TIMEOUT  # noqa: SLF001

    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    kernel32 = FakeKernel32()
    process = DummyProcess()
    monkeypatch.setattr(process_tools_module.os, "name", "nt")
    monkeypatch.setattr(process_tools_module, "_windows_kernel32", lambda: kernel32)
    monkeypatch.setattr(process_tools_module.subprocess, "run", fake_run)

    process_tools_module._kill_process_tree(process)  # noqa: SLF001

    assert kernel32.calls == [("wait", 456, 0)]
    assert calls == [["taskkill", "/F", "/T", "/PID", "123"]]
    assert process.killed is False


def test_bash_tool_skips_taskkill_after_windows_job_termination(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        returncode = 0
        pid = 123
        _handle = 456
        killed = False

        def kill(self) -> None:
            self.killed = True

    class FakeKernel32:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def CreateJobObjectW(self, _attributes: object, _name: object) -> int:
            self.calls.append(("create",))
            return 789

        def AssignProcessToJobObject(self, job: int, process: int) -> int:
            self.calls.append(("assign", job, process))
            return 1

        def TerminateJobObject(self, job: int, exit_code: int) -> int:
            self.calls.append(("terminate", job, exit_code))
            return 1

        def CloseHandle(self, handle: int) -> int:
            self.calls.append(("close", handle))
            return 1

    taskkill_calls: list[list[str]] = []
    kernel32 = FakeKernel32()
    process = DummyProcess()
    monkeypatch.setattr(process_tools_module.os, "name", "nt")
    monkeypatch.setattr(process_tools_module, "_windows_kernel32", lambda: kernel32)

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        taskkill_calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(process_tools_module.subprocess, "run", fake_run)

    process_tools_module._attach_windows_job(process)  # type: ignore[arg-type]  # noqa: SLF001
    process_tools_module._kill_process_tree(process)  # type: ignore[arg-type]  # noqa: SLF001

    assert kernel32.calls == [
        ("create",),
        ("assign", 789, 456),
        ("terminate", 789, 1),
        ("close", 789),
    ]
    assert taskkill_calls == []
    assert process.killed is False


def test_bash_tool_retains_windows_job_handle_when_termination_fails(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        returncode = None
        pid = 123
        _handle = 456
        killed = False

        def kill(self) -> None:
            self.killed = True

    class FakeKernel32:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def CreateJobObjectW(self, _attributes: object, _name: object) -> int:
            self.calls.append(("create",))
            return 789

        def AssignProcessToJobObject(self, job: int, process: int) -> int:
            self.calls.append(("assign", job, process))
            return 1

        def TerminateJobObject(self, job: int, exit_code: int) -> int:
            self.calls.append(("terminate", job, exit_code))
            return 0

        def WaitForSingleObject(self, process: int, milliseconds: int) -> int:
            self.calls.append(("wait", process, milliseconds))
            return process_tools_module._WINDOWS_WAIT_TIMEOUT  # noqa: SLF001

        def CloseHandle(self, handle: int) -> int:
            self.calls.append(("close", handle))
            return 1

    taskkill_calls: list[list[str]] = []
    kernel32 = FakeKernel32()
    process = DummyProcess()
    monkeypatch.setattr(process_tools_module.os, "name", "nt")
    monkeypatch.setattr(process_tools_module, "_windows_kernel32", lambda: kernel32)

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        taskkill_calls.append(command)
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(process_tools_module.subprocess, "run", fake_run)

    process_tools_module._attach_windows_job(process)  # type: ignore[arg-type]  # noqa: SLF001
    terminated = process_tools_module._kill_process_tree(process)  # type: ignore[arg-type]  # noqa: SLF001

    assert terminated is False
    assert getattr(process, process_tools_module._WINDOWS_JOB_HANDLE_ATTR) == 789  # noqa: SLF001
    assert kernel32.calls == [
        ("create",),
        ("assign", 789, 456),
        ("terminate", 789, 1),
        ("wait", 456, 0),
    ]
    assert taskkill_calls == [["taskkill", "/F", "/T", "/PID", "123"]]
    assert process.killed is True


def test_bash_tool_closes_windows_job_handle_after_taskkill_fallback(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        returncode = None
        pid = 123
        _handle = 456
        killed = False

        def kill(self) -> None:
            self.killed = True

    class FakeKernel32:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def CreateJobObjectW(self, _attributes: object, _name: object) -> int:
            self.calls.append(("create",))
            return 789

        def AssignProcessToJobObject(self, job: int, process: int) -> int:
            self.calls.append(("assign", job, process))
            return 1

        def TerminateJobObject(self, job: int, exit_code: int) -> int:
            self.calls.append(("terminate", job, exit_code))
            return 0

        def WaitForSingleObject(self, process: int, milliseconds: int) -> int:
            self.calls.append(("wait", process, milliseconds))
            return process_tools_module._WINDOWS_WAIT_TIMEOUT  # noqa: SLF001

        def CloseHandle(self, handle: int) -> int:
            self.calls.append(("close", handle))
            return 1

    taskkill_calls: list[list[str]] = []
    kernel32 = FakeKernel32()
    process = DummyProcess()
    monkeypatch.setattr(process_tools_module.os, "name", "nt")
    monkeypatch.setattr(process_tools_module, "_windows_kernel32", lambda: kernel32)

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        taskkill_calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(process_tools_module.subprocess, "run", fake_run)

    process_tools_module._attach_windows_job(process)  # type: ignore[arg-type]  # noqa: SLF001
    terminated = process_tools_module._kill_process_tree(process)  # type: ignore[arg-type]  # noqa: SLF001

    assert terminated is True
    assert getattr(process, process_tools_module._WINDOWS_JOB_HANDLE_ATTR) is None  # noqa: SLF001
    assert taskkill_calls == [["taskkill", "/F", "/T", "/PID", "123"]]
    assert kernel32.calls == [
        ("create",),
        ("assign", 789, 456),
        ("terminate", 789, 1),
        ("wait", 456, 0),
        ("close", 789),
    ]
    assert process.killed is False


def test_bash_tool_assigns_windows_job_before_resuming_shell(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    class DummyProcess:
        returncode = None
        pid = 123
        _handle = 456

        async def wait(self) -> int:
            return 0

    class FakeKernel32:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def CreateJobObjectW(self, _attributes: object, _name: object) -> int:
            self.calls.append(("create_job",))
            return 789

        def AssignProcessToJobObject(self, job: int, process: int) -> int:
            self.calls.append(("assign", job, process))
            return 1

        def CreateToolhelp32Snapshot(self, flags: int, process_id: int) -> int:
            self.calls.append(("snapshot", flags, process_id))
            return 111

        def Thread32First(
            self,
            snapshot: int,
            entry: object,
        ) -> int:
            self.calls.append(("thread_first", snapshot))
            entry.contents.th32OwnerProcessID = 123  # type: ignore[attr-defined]
            entry.contents.th32ThreadID = 654  # type: ignore[attr-defined]
            return 1

        def Thread32Next(self, snapshot: int, _entry: object) -> int:
            self.calls.append(("thread_next", snapshot))
            return 0

        def OpenThread(self, access: int, inherit: bool, thread_id: int) -> int:
            self.calls.append(("open_thread", access, inherit, thread_id))
            return 222

        def ResumeThread(self, thread: int) -> int:
            self.calls.append(("resume", thread))
            return 1

        def CloseHandle(self, handle: int) -> int:
            self.calls.append(("close", handle))
            return 1

    creation_calls: list[dict[str, object]] = []
    process = DummyProcess()
    kernel32 = FakeKernel32()

    async def fake_create_subprocess_shell(
        _command: str,
        **kwargs: object,
    ) -> DummyProcess:
        creation_calls.append(kwargs)
        return process

    monkeypatch.setattr(process_tools_module.os, "name", "nt")
    monkeypatch.setattr(
        process_tools_module.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )
    monkeypatch.setattr(process_tools_module, "_windows_kernel32", lambda: kernel32)

    async def run() -> DummyProcess:
        return await process_tools_module._create_shell_process("echo hi", cwd=tmp_path)  # type: ignore[return-value]  # noqa: SLF001

    result = anyio.run(run)

    assert result is process
    assert creation_calls == [
        {
            "cwd": str(tmp_path),
            "start_new_session": False,
            "stdout": process_tools_module.asyncio.subprocess.PIPE,
            "stderr": process_tools_module.asyncio.subprocess.PIPE,
            "creationflags": process_tools_module._WINDOWS_CREATE_SUSPENDED,  # noqa: SLF001
        }
    ]
    assert kernel32.calls == [
        ("create_job",),
        ("assign", 789, 456),
        ("snapshot", process_tools_module._WINDOWS_TH32CS_SNAPTHREAD, 0),  # noqa: SLF001
        ("thread_first", 111),
        (
            "open_thread",
            process_tools_module._WINDOWS_THREAD_SUSPEND_RESUME,  # noqa: SLF001
            False,
            654,
        ),
        ("close", 111),
        ("resume", 222),
        ("close", 222),
    ]


def test_bash_tool_runs_windows_resume_setup_off_event_loop(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    class DummyProcess:
        returncode = None
        pid = 123
        _handle = 456

        async def wait(self) -> int:
            return 0

    class FakeKernel32:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def CreateJobObjectW(self, _attributes: object, _name: object) -> int:
            self.calls.append(("create_job",))
            return 789

        def AssignProcessToJobObject(self, job: int, process: int) -> int:
            self.calls.append(("assign", job, process))
            return 1

        def ResumeThread(self, thread: int) -> int:
            self.calls.append(("resume", thread))
            return 1

        def CloseHandle(self, handle: int) -> int:
            self.calls.append(("close", handle))
            return 1

    process = DummyProcess()
    kernel32 = FakeKernel32()
    open_thread_ids: list[int] = []

    async def fake_create_subprocess_shell(
        _command: str,
        **_kwargs: object,
    ) -> DummyProcess:
        return process

    def fake_open_windows_process_thread(
        _process: object,
        _kernel32: object,
    ) -> int:
        open_thread_ids.append(threading.get_ident())
        return 222

    monkeypatch.setattr(process_tools_module.os, "name", "nt")
    monkeypatch.setattr(
        process_tools_module.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )
    monkeypatch.setattr(process_tools_module, "_windows_kernel32", lambda: kernel32)
    monkeypatch.setattr(
        process_tools_module,
        "_open_windows_process_thread",
        fake_open_windows_process_thread,
    )

    async def run() -> tuple[DummyProcess, int]:
        event_loop_thread_id = threading.get_ident()
        result = await process_tools_module._create_shell_process("echo hi", cwd=tmp_path)  # type: ignore[return-value]  # noqa: SLF001
        return result, event_loop_thread_id

    result, event_loop_thread_id = anyio.run(run)

    assert result is process
    assert open_thread_ids
    assert all(thread_id != event_loop_thread_id for thread_id in open_thread_ids)
    assert kernel32.calls == [
        ("create_job",),
        ("assign", 789, 456),
        ("resume", 222),
        ("close", 222),
    ]


def test_bash_tool_aborts_suspended_windows_shell_when_job_assignment_fails(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    class DummyProcess:
        returncode = None
        pid = 123
        _handle = 456
        killed = False

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.returncode = 1
            return 1

    class FakeKernel32:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def CreateJobObjectW(self, _attributes: object, _name: object) -> int:
            self.calls.append(("create_job",))
            return 789

        def AssignProcessToJobObject(self, job: int, process: int) -> int:
            self.calls.append(("assign", job, process))
            return 0

        def CloseHandle(self, handle: int) -> int:
            self.calls.append(("close", handle))
            return 1

    process = DummyProcess()
    kernel32 = FakeKernel32()

    async def fake_create_subprocess_shell(
        _command: str,
        **_kwargs: object,
    ) -> DummyProcess:
        return process

    monkeypatch.setattr(process_tools_module.os, "name", "nt")
    monkeypatch.setattr(
        process_tools_module.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )
    monkeypatch.setattr(process_tools_module, "_windows_kernel32", lambda: kernel32)

    async def run() -> None:
        await process_tools_module._create_shell_process("echo hi", cwd=tmp_path)  # noqa: SLF001

    with pytest.raises(ToolError, match="Failed to attach command process to Windows job"):
        anyio.run(run)

    assert kernel32.calls == [
        ("create_job",),
        ("assign", 789, 456),
        ("close", 789),
    ]
    assert process.killed is True


def test_bash_tool_cleans_windows_job_after_resume_setup_failure(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        returncode = None
        pid = 123
        _handle = 456
        killed = False

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.returncode = 1
            return 1

    class FakeKernel32:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def CreateJobObjectW(self, _attributes: object, _name: object) -> int:
            self.calls.append(("create",))
            return 789

        def AssignProcessToJobObject(self, job: int, process: int) -> int:
            self.calls.append(("assign", job, process))
            return 1

        def TerminateJobObject(self, job: int, exit_code: int) -> int:
            self.calls.append(("terminate", job, exit_code))
            return 0

        def WaitForSingleObject(self, process: int, milliseconds: int) -> int:
            self.calls.append(("wait", process, milliseconds))
            return process_tools_module._WINDOWS_WAIT_TIMEOUT  # noqa: SLF001

        def CloseHandle(self, handle: int) -> int:
            self.calls.append(("close", handle))
            return 1

    taskkill_calls: list[list[str]] = []
    kernel32 = FakeKernel32()
    process = DummyProcess()
    monkeypatch.setattr(process_tools_module.os, "name", "nt")
    monkeypatch.setattr(process_tools_module, "_windows_kernel32", lambda: kernel32)

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        taskkill_calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(process_tools_module.subprocess, "run", fake_run)

    process_tools_module._attach_windows_job(process)  # type: ignore[arg-type]  # noqa: SLF001
    cleanup_succeeded = anyio.run(
        process_tools_module._cleanup_failed_windows_process_setup,  # type: ignore[arg-type]  # noqa: SLF001
        process,
    )

    assert cleanup_succeeded is True
    assert getattr(process, process_tools_module._WINDOWS_JOB_HANDLE_ATTR) is None  # noqa: SLF001
    assert taskkill_calls == [["taskkill", "/F", "/T", "/PID", "123"]]
    assert kernel32.calls == [
        ("create",),
        ("assign", 789, 456),
        ("terminate", 789, 1),
        ("wait", 456, 0),
        ("close", 789),
    ]


def test_bash_tool_surfaces_windows_setup_cleanup_failure_after_cancellation(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pass

    async def run() -> None:
        setup_started = asyncio.Event()
        allow_setup_finish = asyncio.Event()

        async def fake_to_thread(_function: object, _process: object) -> None:
            setup_started.set()
            await allow_setup_finish.wait()

        async def fail_cleanup(_process: object) -> bool:
            return False

        monkeypatch.setattr(process_tools_module.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(
            process_tools_module, "_cleanup_failed_windows_process_setup", fail_cleanup
        )

        setup_task = asyncio.create_task(
            process_tools_module._run_windows_process_setup(DummyProcess())  # type: ignore[arg-type]  # noqa: SLF001
        )
        await setup_started.wait()
        setup_task.cancel()
        allow_setup_finish.set()
        with pytest.raises(ToolError, match="terminate process tree"):
            await setup_task

    anyio.run(run)


def test_bash_tool_finishes_windows_setup_cleanup_after_repeated_cancellation(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pass

    async def run() -> bool:
        setup_started = asyncio.Event()
        allow_setup_finish = asyncio.Event()
        cleanup_started = asyncio.Event()
        allow_cleanup_finish = asyncio.Event()
        cleanup_finished = False

        async def fake_to_thread(_function: object, _process: object) -> None:
            setup_started.set()
            await allow_setup_finish.wait()

        async def cleanup(_process: object) -> bool:
            nonlocal cleanup_finished
            cleanup_started.set()
            await allow_cleanup_finish.wait()
            cleanup_finished = True
            return True

        monkeypatch.setattr(process_tools_module.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(process_tools_module, "_cleanup_failed_windows_process_setup", cleanup)

        setup_task = asyncio.create_task(
            process_tools_module._run_windows_process_setup(DummyProcess())  # type: ignore[arg-type]  # noqa: SLF001
        )
        await setup_started.wait()
        setup_task.cancel()
        allow_setup_finish.set()
        await cleanup_started.wait()
        setup_task.cancel()
        await asyncio.sleep(0)
        assert not setup_task.done()
        allow_cleanup_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await setup_task
        return cleanup_finished

    assert anyio.run(run) is True


def test_bash_tool_delays_cancellation_during_windows_setup_error_cleanup(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    class DummyProcess:
        pass

    async def run() -> bool:
        cleanup_started = asyncio.Event()
        allow_cleanup_finish = asyncio.Event()
        cleanup_finished = False
        process = DummyProcess()

        async def fake_create_subprocess_shell(
            _command: str,
            **_kwargs: object,
        ) -> DummyProcess:
            return process

        async def setup(_process: object) -> str:
            return "Failed to resume command process"

        async def cleanup(_process: object) -> bool:
            nonlocal cleanup_finished
            cleanup_started.set()
            await allow_cleanup_finish.wait()
            cleanup_finished = True
            return True

        monkeypatch.setattr(process_tools_module.os, "name", "nt")
        monkeypatch.setattr(
            process_tools_module.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
        )
        monkeypatch.setattr(process_tools_module, "_run_windows_process_setup", setup)
        monkeypatch.setattr(process_tools_module, "_cleanup_failed_windows_process_setup", cleanup)

        setup_task = asyncio.create_task(
            process_tools_module._create_shell_process("echo hi", cwd=tmp_path)  # noqa: SLF001
        )
        await cleanup_started.wait()
        setup_task.cancel()
        await asyncio.sleep(0)
        assert not setup_task.done()
        allow_cleanup_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await setup_task
        return cleanup_finished

    assert anyio.run(run) is True


def test_windows_kernel32_configures_pointer_sized_job_api_signatures(
    monkeypatch: MonkeyPatch,
) -> None:
    class FakeFunction:
        def __init__(self, result: int) -> None:
            self.result = result
            self.restype: object = None
            self.argtypes: list[object] | None = None

        def __call__(self, *_args: object) -> int:
            return self.result

    class FakeKernel32:
        def __init__(self) -> None:
            self.CreateJobObjectW = FakeFunction(789)
            self.AssignProcessToJobObject = FakeFunction(1)
            self.TerminateJobObject = FakeFunction(1)
            self.CloseHandle = FakeFunction(1)
            self.CreateToolhelp32Snapshot = FakeFunction(1)
            self.OpenThread = FakeFunction(1)
            self.ResumeThread = FakeFunction(1)
            self.Thread32First = FakeFunction(1)
            self.Thread32Next = FakeFunction(0)
            self.WaitForSingleObject = FakeFunction(process_tools_module._WINDOWS_WAIT_TIMEOUT)  # noqa: SLF001

    kernel32 = FakeKernel32()

    def fake_windll(name: str, *, use_last_error: bool) -> FakeKernel32:
        assert name == "kernel32"
        assert use_last_error is True
        return kernel32

    monkeypatch.setattr(process_tools_module.ctypes, "WinDLL", fake_windll, raising=False)

    assert process_tools_module._windows_kernel32() is kernel32  # noqa: SLF001
    assert kernel32.CreateJobObjectW.restype is process_tools_module.wintypes.HANDLE
    assert kernel32.CreateJobObjectW.argtypes == [
        process_tools_module.ctypes.c_void_p,
        process_tools_module.wintypes.LPCWSTR,
    ]
    assert kernel32.AssignProcessToJobObject.restype is process_tools_module.wintypes.BOOL
    assert kernel32.AssignProcessToJobObject.argtypes == [
        process_tools_module.wintypes.HANDLE,
        process_tools_module.wintypes.HANDLE,
    ]
    assert kernel32.TerminateJobObject.restype is process_tools_module.wintypes.BOOL
    assert kernel32.TerminateJobObject.argtypes == [
        process_tools_module.wintypes.HANDLE,
        process_tools_module.wintypes.UINT,
    ]
    assert kernel32.CloseHandle.restype is process_tools_module.wintypes.BOOL
    assert kernel32.CloseHandle.argtypes == [process_tools_module.wintypes.HANDLE]
    assert kernel32.CreateToolhelp32Snapshot.restype is process_tools_module.wintypes.HANDLE
    assert kernel32.CreateToolhelp32Snapshot.argtypes == [
        process_tools_module.wintypes.DWORD,
        process_tools_module.wintypes.DWORD,
    ]
    assert kernel32.OpenThread.restype is process_tools_module.wintypes.HANDLE
    assert kernel32.OpenThread.argtypes == [
        process_tools_module.wintypes.DWORD,
        process_tools_module.wintypes.BOOL,
        process_tools_module.wintypes.DWORD,
    ]
    assert kernel32.ResumeThread.restype is process_tools_module.wintypes.DWORD
    assert kernel32.ResumeThread.argtypes == [process_tools_module.wintypes.HANDLE]
    assert kernel32.Thread32First.restype is process_tools_module.wintypes.BOOL
    assert kernel32.Thread32First.argtypes == [
        process_tools_module.wintypes.HANDLE,
        process_tools_module.ctypes.POINTER(process_tools_module._WindowsThreadEntry32),  # noqa: SLF001
    ]
    assert kernel32.Thread32Next.restype is process_tools_module.wintypes.BOOL
    assert kernel32.Thread32Next.argtypes == [
        process_tools_module.wintypes.HANDLE,
        process_tools_module.ctypes.POINTER(process_tools_module._WindowsThreadEntry32),  # noqa: SLF001
    ]
    assert kernel32.WaitForSingleObject.restype is process_tools_module.wintypes.DWORD
    assert kernel32.WaitForSingleObject.argtypes == [
        process_tools_module.wintypes.HANDLE,
        process_tools_module.wintypes.DWORD,
    ]


def test_bash_tool_skips_taskkill_for_exited_windows_leader_without_job(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        returncode = 0
        pid = 123
        killed = False

        def kill(self) -> None:
            self.killed = True

    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    process = DummyProcess()
    monkeypatch.setattr(process_tools_module.os, "name", "nt")
    monkeypatch.setattr(process_tools_module.subprocess, "run", fake_run)

    process_tools_module._kill_process_tree(process)  # type: ignore[arg-type]  # noqa: SLF001

    assert calls == []
    assert process.killed is False


# Asserts a wall-clock margin, so it stays out of the parallel suite.
@pytest.mark.process
def test_async_windows_tree_cleanup_does_not_block_event_loop(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        returncode = None
        pid = 123

    monkeypatch.setattr(process_tools_module.os, "name", "nt")

    def slow_kill(_process: object) -> None:
        time.sleep(0.1)

    monkeypatch.setattr(process_tools_module, "_kill_process_tree", slow_kill)

    async def run() -> bool:
        completed = anyio.Event()

        async def terminate() -> None:
            await process_tools_module._terminate_process_tree(DummyProcess())  # type: ignore[arg-type]  # noqa: SLF001
            completed.set()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(terminate)
            await anyio.sleep(0.01)
            event_loop_remained_responsive = not completed.is_set()
        return event_loop_remained_responsive

    assert anyio.run(run) is True
