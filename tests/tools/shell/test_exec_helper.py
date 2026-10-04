from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from wisp.tools.result import ToolError
from wisp.tools.shell import process as process_tools_module
from wisp.tools.shell import supervisor as process_manager_module

pytestmark = pytest.mark.process


def test_exec_helper_bounds_stderr_before_buffering(tmp_path: Path) -> None:
    async def run() -> process_tools_module.ProcessResult:
        supervisor = process_manager_module.ProcessSupervisor()
        return await process_tools_module._run_exec_limited_stdout(  # noqa: SLF001
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('e' * 10000)",
            ],
            cwd=tmp_path,
            process_supervisor=supervisor,
            max_stdout_lines=1,
            max_buffered_stderr_bytes=20,
            max_buffered_stderr_lines=100,
        )

    result = anyio.run(run)

    assert len(result.stderr.encode("utf-8")) <= 20
    assert result.stderr_truncated is True


def test_exec_helper_reports_failed_output_limit_termination(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(b"first\nsecond\n")
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()
            self.returncode = None
            self.wait_called = False

        async def wait(self) -> int:
            self.wait_called = True
            if self.returncode is not None:
                return self.returncode
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    process: DummyProcess | None = None

    async def fake_spawn(*_args: object, **_kwargs: object) -> DummyProcess:
        nonlocal process
        process = DummyProcess()
        return process

    cleanup_succeeds = False

    async def fail_terminate(_process: object) -> bool:
        if cleanup_succeeds:
            assert process is not None
            process.returncode = -9
            return True
        return False

    monkeypatch.setattr(process_tools_module.asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(process_tools_module, "_terminate_process_tree", fail_terminate)
    monkeypatch.setattr(process_manager_module, "_terminate_process_tree", fail_terminate)

    async def run() -> int:
        nonlocal cleanup_succeeds
        supervisor = process_manager_module.ProcessSupervisor()

        async def execute() -> None:
            await process_tools_module._run_exec_limited_stdout(  # noqa: SLF001
                ["command"],
                cwd=tmp_path,
                process_supervisor=supervisor,
                max_stdout_lines=10,
                max_buffered_stdout_lines=1,
            )

        with anyio.fail_after(1):
            with pytest.raises(ToolError, match="Failed to terminate process tree"):
                await asyncio.create_task(execute())
        retained = len(supervisor._one_shot)  # noqa: SLF001
        cleanup_succeeds = True
        await supervisor.aclose()
        return retained

    retained = anyio.run(run)

    assert process is not None
    assert process.wait_called is True
    assert retained == 1


def test_exec_helper_cleans_process_when_cancelled_during_registration(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    original_spawn = process_tools_module.asyncio.create_subprocess_exec
    process: asyncio.subprocess.Process | None = None
    spawned = asyncio.Event()
    registration_started = asyncio.Event()
    allow_registration = asyncio.Event()

    async def record_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        nonlocal process
        process = await original_spawn(*args, **kwargs)
        spawned.set()
        return process

    monkeypatch.setattr(process_tools_module.asyncio, "create_subprocess_exec", record_spawn)

    async def run() -> tuple[int | None, int]:
        supervisor = process_manager_module.ProcessSupervisor()
        original_track = supervisor._track_one_shot  # noqa: SLF001

        async def delayed_track(
            tracked_process: asyncio.subprocess.Process,
            task: asyncio.Task[object],
            *,
            reserved: bool = False,
        ) -> None:
            registration_started.set()
            await allow_registration.wait()
            await original_track(tracked_process, task, reserved=reserved)

        monkeypatch.setattr(supervisor, "_track_one_shot", delayed_track)
        execute = asyncio.create_task(
            process_tools_module._run_exec_limited_stdout(  # noqa: SLF001
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=tmp_path,
                process_supervisor=supervisor,
                max_stdout_lines=10,
            )
        )
        await spawned.wait()
        await registration_started.wait()
        execute.cancel()
        await anyio.sleep(0)
        assert execute.done() is False

        allow_registration.set()
        with pytest.raises(asyncio.CancelledError):
            await execute
        assert process is not None
        return process.returncode, len(supervisor._one_shot)  # noqa: SLF001

    returncode, retained = anyio.run(run)

    assert returncode is not None
    assert retained == 0


def test_exec_helper_cleans_process_when_registration_finds_closed_supervisor(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    original_spawn = process_tools_module.asyncio.create_subprocess_exec
    process: asyncio.subprocess.Process | None = None

    async def record_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        nonlocal process
        process = await original_spawn(*args, **kwargs)
        return process

    monkeypatch.setattr(process_tools_module.asyncio, "create_subprocess_exec", record_spawn)

    async def run() -> int:
        supervisor = process_manager_module.ProcessSupervisor()
        await supervisor.aclose()
        with pytest.raises(RuntimeError, match="ProcessSupervisor is closed"):
            await process_tools_module._run_exec_limited_stdout(  # noqa: SLF001
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=tmp_path,
                process_supervisor=supervisor,
                max_stdout_lines=10,
            )
        return len(supervisor._one_shot)  # noqa: SLF001

    retained = anyio.run(run)

    assert process is None
    assert retained == 0


def test_exec_helper_finishes_reservation_rollback_after_repeated_cancel(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    spawn_started = asyncio.Event()

    async def pending_spawn(*_args: object, **_kwargs: object) -> asyncio.subprocess.Process:
        spawn_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(process_tools_module.asyncio, "create_subprocess_exec", pending_spawn)

    async def run() -> int:
        supervisor = process_manager_module.ProcessSupervisor()
        execute = asyncio.create_task(
            process_tools_module._run_exec_limited_stdout(  # noqa: SLF001
                [sys.executable, "-c", "pass"],
                cwd=tmp_path,
                process_supervisor=supervisor,
                max_stdout_lines=10,
            )
        )
        await spawn_started.wait()
        async with supervisor._lock:  # noqa: SLF001
            execute.cancel()
            await anyio.sleep(0)
            execute.cancel()
            await anyio.sleep(0)
            assert execute.done() is False

        with pytest.raises(asyncio.CancelledError):
            await execute
        with anyio.fail_after(1):
            await supervisor.aclose()
        return supervisor._pending_one_shot_starts  # noqa: SLF001

    assert anyio.run(run) == 0


def test_aclose_waits_for_one_shot_reserved_before_close(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    original_spawn = process_tools_module.asyncio.create_subprocess_exec
    process: asyncio.subprocess.Process | None = None
    spawn_started = asyncio.Event()
    allow_spawn = asyncio.Event()

    async def delayed_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        nonlocal process
        spawn_started.set()
        await allow_spawn.wait()
        process = await original_spawn(*args, **kwargs)
        return process

    monkeypatch.setattr(process_tools_module.asyncio, "create_subprocess_exec", delayed_spawn)

    async def run() -> int:
        supervisor = process_manager_module.ProcessSupervisor()
        execute = asyncio.create_task(
            process_tools_module._run_exec_limited_stdout(  # noqa: SLF001
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=tmp_path,
                process_supervisor=supervisor,
                max_stdout_lines=10,
            )
        )
        await spawn_started.wait()
        close_task = asyncio.create_task(supervisor.aclose())
        await anyio.sleep(0.05)
        assert close_task.done() is False

        allow_spawn.set()
        with pytest.raises(RuntimeError, match="ProcessSupervisor is closed"):
            await execute
        await close_task
        assert process is not None
        assert process.returncode is not None
        return len(supervisor._one_shot)  # noqa: SLF001

    retained = anyio.run(run)

    assert retained == 0


def test_exec_helper_delays_repeated_cancellation_until_cleanup_finishes(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    supervisor = process_manager_module.ProcessSupervisor()
    original_terminate = supervisor._terminate_one_shot  # noqa: SLF001
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()

    async def delayed_terminate(
        process: asyncio.subprocess.Process,
        *,
        wait: bool = False,
    ) -> bool:
        cleanup_started.set()
        await allow_cleanup.wait()
        return await original_terminate(process, wait=wait)

    monkeypatch.setattr(supervisor, "_terminate_one_shot", delayed_terminate)

    async def run() -> int:
        execute = asyncio.create_task(
            process_tools_module._run_exec_limited_stdout(  # noqa: SLF001
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=tmp_path,
                process_supervisor=supervisor,
                max_stdout_lines=10,
            )
        )
        with anyio.fail_after(1):
            while not supervisor._one_shot:  # noqa: SLF001
                await anyio.sleep(0)
        execute.cancel()
        await cleanup_started.wait()
        execute.cancel()
        await anyio.sleep(0)
        assert execute.done() is False

        allow_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await execute
        return len(supervisor._one_shot)  # noqa: SLF001

    retained = anyio.run(run)

    assert retained == 0
