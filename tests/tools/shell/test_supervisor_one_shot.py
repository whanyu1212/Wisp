from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path

import anyio
import pytest

import wisp.tools.shell.supervisor as process_manager_module
from tests.tools.shell.supervisor_support import (
    python_command,
    wait_for_pid_file,
)
from wisp.tools.result import ToolError
from wisp.tools.shell.supervisor import (
    ProcessSupervisor,
)

pytestmark = pytest.mark.process


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group assertion")
def test_one_shot_completion_kills_descendant_with_redirected_output(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "background.pid"
    command = f"sleep 30 >/dev/null 2>&1 & echo $! > {shlex.quote(str(child_pid_path))}"

    async def run() -> tuple[int, str, int]:
        supervisor = ProcessSupervisor()
        try:
            result = await supervisor.run_to_completion(
                command,
                cwd=tmp_path,
                timeout=2,
                max_output_bytes=1_000,
                max_output_lines=100,
            )
            child_pid = await wait_for_pid_file(child_pid_path)
            with anyio.fail_after(3):
                while True:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        break
                    await anyio.sleep(0.01)
            return result.exit_code, result.stdout, child_pid
        finally:
            await supervisor.aclose()

    exit_code, stdout, child_pid = anyio.run(run)

    assert exit_code == 0
    assert stdout == ""
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_one_shot_completion_surfaces_process_tree_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_terminate(_process: asyncio.subprocess.Process) -> bool:
        return False

    monkeypatch.setattr(process_manager_module, "_terminate_process_tree", fail_terminate)

    async def run() -> int:
        supervisor = ProcessSupervisor()
        try:
            with pytest.raises(ToolError, match="Failed to terminate process tree"):
                await supervisor.run_to_completion(
                    python_command("print('done')"),
                    cwd=tmp_path,
                    timeout=2,
                    max_output_bytes=1_000,
                    max_output_lines=100,
                )
            return len(supervisor._one_shot)  # noqa: SLF001
        finally:
            try:
                await supervisor.aclose()
            except ToolError as exc:
                assert str(exc) == "Failed to terminate process tree"
                pass

    retained_count = anyio.run(run)

    assert retained_count == 1


def test_one_shot_timeout_retains_ownership_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_cleanup = process_manager_module._kill_process_tree_and_wait  # type: ignore[attr-defined]

    async def fail_cleanup(process: asyncio.subprocess.Process) -> bool:
        await original_cleanup(process)
        return False

    monkeypatch.setattr(process_manager_module, "_kill_process_tree_and_wait", fail_cleanup)

    async def run() -> int:
        supervisor = ProcessSupervisor()
        try:
            with pytest.raises(ToolError, match="Failed to terminate process tree"):
                await supervisor.run_to_completion(
                    python_command("import time; time.sleep(30)"),
                    cwd=tmp_path,
                    timeout=0.05,
                    max_output_bytes=1_000,
                    max_output_lines=100,
                )
            return len(supervisor._one_shot)  # noqa: SLF001
        finally:
            await supervisor.aclose()

    retained_count = anyio.run(run)

    assert retained_count == 1


def test_one_shot_capture_error_retains_ownership_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_cleanup = process_manager_module._kill_process_tree_and_wait  # type: ignore[attr-defined]

    async def fail_capture(
        _process: asyncio.subprocess.Process,
        _budget: object,
    ) -> tuple[bytes, bytes]:
        raise OSError("broken pipe")

    async def fail_cleanup(process: asyncio.subprocess.Process) -> bool:
        await original_cleanup(process)
        return False

    monkeypatch.setattr(process_manager_module, "_collect_limited_output", fail_capture)
    monkeypatch.setattr(process_manager_module, "_kill_process_tree_and_wait", fail_cleanup)

    async def run() -> int:
        supervisor = ProcessSupervisor()
        try:
            with pytest.raises(OSError, match="broken pipe"):
                await supervisor.run_to_completion(
                    python_command("import time; time.sleep(30)"),
                    cwd=tmp_path,
                    timeout=30,
                    max_output_bytes=1_000,
                    max_output_lines=100,
                )
            return len(supervisor._one_shot)  # noqa: SLF001
        finally:
            await supervisor.aclose()

    retained_count = anyio.run(run)

    assert retained_count == 1


@pytest.mark.production_fault
def test_one_shot_releases_ownership_inside_cancelled_scope_while_lock_is_contended(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_terminate = process_manager_module._terminate_process_tree  # type: ignore[attr-defined]
    terminate_started = anyio.Event()
    allow_terminate = anyio.Event()

    async def delayed_terminate(process: asyncio.subprocess.Process) -> bool:
        terminate_started.set()
        await allow_terminate.wait()
        return await original_terminate(process)

    monkeypatch.setattr(process_manager_module, "_terminate_process_tree", delayed_terminate)

    async def run() -> tuple[int, str]:
        supervisor = ProcessSupervisor(max_processes=1)
        cancel_scope: anyio.CancelScope | None = None
        one_shot_finished = anyio.Event()
        lock_held = anyio.Event()
        release_lock = anyio.Event()

        async def hold_lock_during_release() -> None:
            await terminate_started.wait()
            async with supervisor._lock:  # noqa: SLF001
                lock_held.set()
                await release_lock.wait()

        async def run_one_shot_from_cancelled_scope() -> None:
            nonlocal cancel_scope
            with anyio.CancelScope() as scope:
                cancel_scope = scope
                await supervisor.run_to_completion(
                    python_command("print('first')"),
                    cwd=tmp_path,
                    timeout=2,
                    max_output_bytes=1_000,
                    max_output_lines=100,
                )
            one_shot_finished.set()

        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(hold_lock_during_release)
                task_group.start_soon(run_one_shot_from_cancelled_scope)
                await lock_held.wait()
                allow_terminate.set()
                await anyio.sleep(0.05)
                assert cancel_scope is not None
                cancel_scope.cancel()
                await anyio.sleep(0.05)
                assert one_shot_finished.is_set() is False
                release_lock.set()

            retained_count = len(supervisor._one_shot)  # noqa: SLF001
            result = await supervisor.run_to_completion(
                python_command("print('second')"),
                cwd=tmp_path,
                timeout=2,
                max_output_bytes=1_000,
                max_output_lines=100,
            )
            return retained_count, result.stdout
        finally:
            await supervisor.aclose()

    retained_count, stdout = anyio.run(run)

    assert retained_count == 0
    assert stdout == "second\n"


def test_one_shot_releases_ownership_after_raw_task_cancel_while_lock_is_contended(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_terminate = process_manager_module._terminate_process_tree  # type: ignore[attr-defined]
    terminate_started = asyncio.Event()
    allow_terminate = asyncio.Event()

    async def delayed_terminate(process: asyncio.subprocess.Process) -> bool:
        terminate_started.set()
        await allow_terminate.wait()
        return await original_terminate(process)

    monkeypatch.setattr(process_manager_module, "_terminate_process_tree", delayed_terminate)

    async def run() -> tuple[int, str]:
        supervisor = ProcessSupervisor(max_processes=1)
        lock_held = asyncio.Event()
        release_lock = asyncio.Event()

        async def hold_lock_during_release() -> None:
            await terminate_started.wait()
            async with supervisor._lock:  # noqa: SLF001
                lock_held.set()
                await release_lock.wait()

        try:
            hold_lock_task = asyncio.create_task(hold_lock_during_release())
            run_task = asyncio.create_task(
                supervisor.run_to_completion(
                    python_command("print('first')"),
                    cwd=tmp_path,
                    timeout=2,
                    max_output_bytes=1_000,
                    max_output_lines=100,
                )
            )
            await lock_held.wait()
            allow_terminate.set()
            await anyio.sleep(0.05)

            run_task.cancel()
            await anyio.sleep(0.05)
            assert run_task.done() is False

            release_lock.set()
            await hold_lock_task
            with pytest.raises(asyncio.CancelledError):
                await run_task

            retained_count = len(supervisor._one_shot)  # noqa: SLF001
            result = await supervisor.run_to_completion(
                python_command("print('second')"),
                cwd=tmp_path,
                timeout=2,
                max_output_bytes=1_000,
                max_output_lines=100,
            )
            return retained_count, result.stdout
        finally:
            await supervisor.aclose()

    retained_count, stdout = anyio.run(run)

    assert retained_count == 0
    assert stdout == "second\n"


def test_one_shot_releases_ownership_after_raw_task_cancel_during_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_terminate = process_manager_module._terminate_process_tree  # type: ignore[attr-defined]
    terminate_started = asyncio.Event()
    allow_terminate = asyncio.Event()

    async def delayed_terminate(process: asyncio.subprocess.Process) -> bool:
        terminate_started.set()
        await allow_terminate.wait()
        return await original_terminate(process)

    monkeypatch.setattr(process_manager_module, "_terminate_process_tree", delayed_terminate)

    async def run() -> tuple[int, str]:
        supervisor = ProcessSupervisor(max_processes=1)
        try:
            run_task = asyncio.create_task(
                supervisor.run_to_completion(
                    python_command("print('first')"),
                    cwd=tmp_path,
                    timeout=2,
                    max_output_bytes=1_000,
                    max_output_lines=100,
                )
            )
            await terminate_started.wait()
            run_task.cancel()
            await anyio.sleep(0.05)
            assert run_task.done() is False

            allow_terminate.set()
            with pytest.raises(asyncio.CancelledError):
                await run_task

            retained_count = len(supervisor._one_shot)  # noqa: SLF001
            result = await supervisor.run_to_completion(
                python_command("print('second')"),
                cwd=tmp_path,
                timeout=2,
                max_output_bytes=1_000,
                max_output_lines=100,
            )
            return retained_count, result.stdout
        finally:
            await supervisor.aclose()

    retained_count, stdout = anyio.run(run)

    assert retained_count == 0
    assert stdout == "second\n"


def test_one_shot_capture_error_terminates_process_before_releasing_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_processes: list[asyncio.subprocess.Process] = []

    async def fail_capture(
        process: asyncio.subprocess.Process,
        _budget: object,
    ) -> tuple[bytes, bytes]:
        captured_processes.append(process)
        raise OSError("broken pipe")

    monkeypatch.setattr(process_manager_module, "_collect_limited_output", fail_capture)

    async def run() -> tuple[int | None, int]:
        supervisor = ProcessSupervisor()
        try:
            with pytest.raises(OSError, match="broken pipe"):
                await supervisor.run_to_completion(
                    python_command("import time; time.sleep(30)"),
                    cwd=tmp_path,
                    timeout=30,
                    max_output_bytes=1_000,
                    max_output_lines=100,
                )
            assert len(captured_processes) == 1
            return captured_processes[0].returncode, len(supervisor._one_shot)  # noqa: SLF001
        finally:
            await supervisor.aclose()

    returncode, retained_count = anyio.run(run)

    assert returncode is not None
    assert retained_count == 0
