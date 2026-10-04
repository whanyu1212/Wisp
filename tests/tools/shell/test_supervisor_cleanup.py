from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import anyio
import pytest

import wisp.tools.shell.supervisor as process_manager_module
from tests.tools.shell.supervisor_support import (
    poll_until_terminal,
    python_command,
    wait_for_pid_file,
)
from wisp.tools.result import ToolError
from wisp.tools.shell.supervisor import (
    ProcessSupervisor,
    ProcessUpdate,
)

pytestmark = pytest.mark.process


def test_managed_timeout_reports_process_tree_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_terminate = process_manager_module._terminate_process_tree  # type: ignore[attr-defined]

    async def fail_terminate(process: asyncio.subprocess.Process) -> bool:
        await original_terminate(process)
        return False

    monkeypatch.setattr(process_manager_module, "_terminate_process_tree", fail_terminate)

    async def run() -> ProcessUpdate:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(
                python_command("import time; time.sleep(30)"),
                cwd=tmp_path,
                timeout=0.05,
            )
            return (await poll_until_terminal(supervisor, process_id))[-1]
        finally:
            try:
                await supervisor.aclose()
            except ToolError as exc:
                assert str(exc) == "Failed to terminate process tree"
                pass

    update = anyio.run(run)

    assert update.state == "failed"
    assert update.exit_code is None
    assert update.error == "Failed to terminate process tree"


def test_one_shot_timeout_reports_process_tree_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_cleanup(_process: asyncio.subprocess.Process) -> bool:
        return False

    monkeypatch.setattr(process_manager_module, "_kill_process_tree_and_wait", fail_cleanup)

    async def run() -> tuple[str, int, int]:
        supervisor = ProcessSupervisor(max_processes=1)
        closed = False
        try:
            with pytest.raises(ToolError) as exc_info:
                await supervisor.run_to_completion(
                    python_command("import time; time.sleep(30)"),
                    cwd=tmp_path,
                    timeout=0.05,
                    max_output_bytes=1_000,
                    max_output_lines=100,
                )
            retained_before_close = len(supervisor._one_shot)  # noqa: SLF001

            with pytest.raises(ToolError, match="managed process limit"):
                await supervisor.run_to_completion(
                    python_command("print('next')"),
                    cwd=tmp_path,
                    timeout=1,
                    max_output_bytes=1_000,
                    max_output_lines=100,
                )

            await supervisor.aclose()
            closed = True
            return str(exc_info.value), retained_before_close, len(supervisor._one_shot)  # noqa: SLF001
        finally:
            if not closed:
                await supervisor.aclose()

    error, retained_before_close, retained_after_close = anyio.run(run)

    assert error == "Failed to terminate process tree"
    assert retained_before_close == 1
    assert retained_after_close == 0


def test_aclose_retries_cleanup_failed_managed_process_before_discarding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_terminate = process_manager_module._terminate_process_tree  # type: ignore[attr-defined]
    cleanup_attempts = 0

    async def fail_once(process: asyncio.subprocess.Process) -> bool:
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        await original_terminate(process)
        return cleanup_attempts > 1

    monkeypatch.setattr(process_manager_module, "_terminate_process_tree", fail_once)

    async def run() -> tuple[ProcessUpdate, int, int]:
        supervisor = ProcessSupervisor()
        process_id = await supervisor.start(
            python_command("import time; time.sleep(30)"),
            cwd=tmp_path,
            timeout=0.05,
        )
        update = (await poll_until_terminal(supervisor, process_id))[-1]
        retained_before_close = len(supervisor._managed)  # noqa: SLF001
        await supervisor.aclose()
        return update, retained_before_close, len(supervisor._managed)  # noqa: SLF001

    update, retained_before_close, retained_after_close = anyio.run(run)

    assert update.state == "failed"
    assert update.error == "Failed to terminate process tree"
    assert retained_before_close == 1
    assert retained_after_close == 0
    assert cleanup_attempts == 2


def test_aclose_surfaces_and_retains_cleanup_failed_managed_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_terminate = process_manager_module._terminate_process_tree  # type: ignore[attr-defined]
    cleanup_attempts = 0
    cleanup_succeeds = False

    async def fail_cleanup(process: asyncio.subprocess.Process) -> bool:
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        await original_terminate(process)
        return cleanup_succeeds

    monkeypatch.setattr(process_manager_module, "_terminate_process_tree", fail_cleanup)

    async def run() -> tuple[ProcessUpdate, int, int, int]:
        nonlocal cleanup_succeeds
        supervisor = ProcessSupervisor()
        process_id = await supervisor.start(
            python_command("import time; time.sleep(30)"),
            cwd=tmp_path,
            timeout=0.05,
        )
        update = (await poll_until_terminal(supervisor, process_id))[-1]
        with pytest.raises(ToolError, match="Failed to terminate process tree"):
            await supervisor.aclose()
        retained_after_failed_close = len(supervisor._managed)  # noqa: SLF001
        cleanup_succeeds = True
        await supervisor.aclose()
        return (
            update,
            cleanup_attempts,
            retained_after_failed_close,
            len(supervisor._managed),  # noqa: SLF001
        )

    update, attempts, retained_after_failed_close, retained_after_retry = anyio.run(run)

    assert update.state == "failed"
    assert update.error == "Failed to terminate process tree"
    assert attempts == 3
    assert retained_after_failed_close == 1
    assert retained_after_retry == 0


def test_cleanup_failed_managed_process_is_not_evicted_after_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_terminate = process_manager_module._terminate_process_tree  # type: ignore[attr-defined]
    cleanup_succeeds = False

    async def fail_until_shutdown_retry(process: asyncio.subprocess.Process) -> bool:
        await original_terminate(process)
        return cleanup_succeeds

    monkeypatch.setattr(
        process_manager_module,
        "_terminate_process_tree",
        fail_until_shutdown_retry,
    )

    async def run() -> tuple[ProcessUpdate, int, int]:
        nonlocal cleanup_succeeds
        supervisor = ProcessSupervisor(max_processes=1)
        process_id = await supervisor.start(
            python_command("import time; time.sleep(30)"),
            cwd=tmp_path,
            timeout=0.05,
        )
        update = (await poll_until_terminal(supervisor, process_id))[-1]
        with pytest.raises(ToolError, match="managed process limit"):
            await supervisor.start(
                python_command("print('blocked')"),
                cwd=tmp_path,
                timeout=2,
            )
        retained_before_retry = len(supervisor._managed)  # noqa: SLF001
        cleanup_succeeds = True
        await supervisor.aclose()
        return update, retained_before_retry, len(supervisor._managed)  # noqa: SLF001

    update, retained_before_retry, retained_after_retry = anyio.run(run)

    assert update.state == "failed"
    assert update.error == "Failed to terminate process tree"
    assert retained_before_retry == 1
    assert retained_after_retry == 0


def test_cancel_retries_cleanup_failed_managed_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_terminate = process_manager_module._terminate_process_tree  # type: ignore[attr-defined]
    cleanup_attempts = 0

    async def fail_until_cancel_retry(process: asyncio.subprocess.Process) -> bool:
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        await original_terminate(process)
        return cleanup_attempts > 1

    monkeypatch.setattr(
        process_manager_module,
        "_terminate_process_tree",
        fail_until_cancel_retry,
    )

    async def run() -> tuple[ProcessUpdate, ProcessUpdate, ProcessUpdate, str, int]:
        supervisor = ProcessSupervisor(max_processes=1)
        try:
            process_id = await supervisor.start(
                python_command("import time; time.sleep(30)"),
                cwd=tmp_path,
                timeout=0.05,
            )
            failed = (await poll_until_terminal(supervisor, process_id))[-1]
            retried = await supervisor.cancel(process_id)
            second_id = await supervisor.start(
                python_command("print('accepted')"),
                cwd=tmp_path,
                timeout=2,
            )
            second_updates = await poll_until_terminal(supervisor, second_id)
            return (
                failed,
                retried,
                second_updates[-1],
                "".join(update.stdout for update in second_updates),
                cleanup_attempts,
            )
        finally:
            await supervisor.aclose()

    failed, retried, second, second_stdout, attempts = anyio.run(run)

    assert failed.state == "failed"
    assert failed.error == "Failed to terminate process tree"
    assert retried.state == "timed_out"
    assert retried.error is None
    assert second.state == "completed"
    assert second_stdout == "accepted\n"
    assert attempts == 3


def test_cancel_retries_after_failed_running_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_terminate = process_manager_module._terminate_process_tree  # type: ignore[attr-defined]
    cleanup_attempts = 0
    cleanup_succeeds = False

    async def fail_until_second_cancel(process: asyncio.subprocess.Process) -> bool:
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        await original_terminate(process)
        return cleanup_succeeds

    monkeypatch.setattr(
        process_manager_module,
        "_terminate_process_tree",
        fail_until_second_cancel,
    )

    async def run() -> tuple[ProcessUpdate, ProcessUpdate, ProcessUpdate, str, int]:
        nonlocal cleanup_succeeds
        supervisor = ProcessSupervisor(max_processes=1)
        try:
            process_id = await supervisor.start(
                python_command("import time; time.sleep(30)"),
                cwd=tmp_path,
                timeout=60,
            )
            failed_cancel = await supervisor.cancel(process_id)
            cleanup_succeeds = True
            retried_cancel = await supervisor.cancel(process_id)
            second_id = await supervisor.start(
                python_command("print('accepted')"),
                cwd=tmp_path,
                timeout=2,
            )
            second_updates = await poll_until_terminal(supervisor, second_id)
            return (
                failed_cancel,
                retried_cancel,
                second_updates[-1],
                "".join(update.stdout for update in second_updates),
                cleanup_attempts,
            )
        finally:
            await supervisor.aclose()

    failed_cancel, retried_cancel, second, second_stdout, attempts = anyio.run(run)

    assert failed_cancel.state == "failed"
    assert failed_cancel.error == "Failed to terminate process tree"
    assert retried_cancel.state == "cancelled"
    assert retried_cancel.error is None
    assert second.state == "completed"
    assert second_stdout == "accepted\n"
    assert attempts == 3


@pytest.mark.production_fault
def test_managed_timeout_bounds_post_termination_stream_drain(tmp_path: Path) -> None:
    async def hold_pipe_open() -> None:
        await asyncio.Event().wait()

    async def run() -> tuple[ProcessUpdate, tuple[asyncio.Task[None], ...]]:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(
                python_command("import time; time.sleep(30)"),
                cwd=tmp_path,
                timeout=0.5,
            )
            managed = supervisor._managed[process_id]  # noqa: SLF001
            assert managed.stdout_task is not None
            assert managed.stderr_task is not None
            managed.stdout_task.cancel()
            managed.stderr_task.cancel()
            await asyncio.gather(
                managed.stdout_task,
                managed.stderr_task,
                return_exceptions=True,
            )

            retained_pipe_tasks = (
                asyncio.create_task(hold_pipe_open()),
                asyncio.create_task(hold_pipe_open()),
            )
            managed.stdout_task, managed.stderr_task = retained_pipe_tasks
            with anyio.fail_after(2):
                update = (await poll_until_terminal(supervisor, process_id))[-1]
            return update, retained_pipe_tasks
        finally:
            await supervisor.aclose()

    update, retained_pipe_tasks = anyio.run(run)

    assert update.state == "timed_out"
    assert update.exit_code is None
    assert all(task.cancelled() for task in retained_pipe_tasks)


def test_supervisor_close_bounds_post_termination_stream_drain(tmp_path: Path) -> None:
    async def hold_pipe_open() -> None:
        await asyncio.Event().wait()

    async def run() -> tuple[asyncio.Task[None], ...]:
        supervisor = ProcessSupervisor()
        process_id = await supervisor.start(
            python_command("import time; time.sleep(30)"),
            cwd=tmp_path,
            timeout=30,
        )
        managed = supervisor._managed[process_id]  # noqa: SLF001
        assert managed.stdout_task is not None
        assert managed.stderr_task is not None
        managed.stdout_task.cancel()
        managed.stderr_task.cancel()
        await asyncio.gather(
            managed.stdout_task,
            managed.stderr_task,
            return_exceptions=True,
        )

        retained_pipe_tasks = (
            asyncio.create_task(hold_pipe_open()),
            asyncio.create_task(hold_pipe_open()),
        )
        managed.stdout_task, managed.stderr_task = retained_pipe_tasks
        with anyio.fail_after(2):
            await supervisor.aclose()
        return retained_pipe_tasks

    retained_pipe_tasks = anyio.run(run)

    assert all(task.cancelled() for task in retained_pipe_tasks)


def test_supervisor_close_bounds_one_shot_capture_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_started = asyncio.Event()

    async def hold_capture(
        _process: asyncio.subprocess.Process,
        _budget: object,
    ) -> tuple[bytes, bytes]:
        capture_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(process_manager_module, "_collect_limited_output", hold_capture)

    async def run() -> tuple[bool, int]:
        supervisor = ProcessSupervisor()
        run_task = asyncio.create_task(
            supervisor.run_to_completion(
                python_command("import time; time.sleep(30)"),
                cwd=tmp_path,
                timeout=30,
                max_output_bytes=1_000,
                max_output_lines=100,
            )
        )
        await capture_started.wait()
        capture_tasks = tuple(supervisor._one_shot.values())  # noqa: SLF001
        assert len(capture_tasks) == 1

        with anyio.fail_after(2):
            await supervisor.aclose()
        with pytest.raises(asyncio.CancelledError):
            await run_task
        return capture_tasks[0].cancelled(), len(supervisor._one_shot)  # noqa: SLF001

    capture_cancelled, retained_count = anyio.run(run)

    assert capture_cancelled is True
    assert retained_count == 0


def test_one_shot_cleanup_serializes_with_supervisor_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_cleanup = process_manager_module._kill_process_tree_and_wait  # type: ignore[attr-defined]
    original_terminate = process_manager_module._terminate_process_tree  # type: ignore[attr-defined]
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    active_cleanups = 0
    max_active_cleanups = 0

    async def enter_cleanup() -> None:
        nonlocal active_cleanups, max_active_cleanups
        active_cleanups += 1
        max_active_cleanups = max(max_active_cleanups, active_cleanups)
        cleanup_started.set()
        await allow_cleanup.wait()

    async def delayed_cleanup(process: asyncio.subprocess.Process) -> bool:
        nonlocal active_cleanups
        await enter_cleanup()
        try:
            return await original_cleanup(process)
        finally:
            active_cleanups -= 1

    async def delayed_terminate(process: asyncio.subprocess.Process) -> bool:
        nonlocal active_cleanups
        await enter_cleanup()
        try:
            return await original_terminate(process)
        finally:
            active_cleanups -= 1

    monkeypatch.setattr(process_manager_module, "_kill_process_tree_and_wait", delayed_cleanup)
    monkeypatch.setattr(process_manager_module, "_terminate_process_tree", delayed_terminate)

    async def run() -> None:
        supervisor = ProcessSupervisor()
        run_task = asyncio.create_task(
            supervisor.run_to_completion(
                python_command("import time; time.sleep(30)"),
                cwd=tmp_path,
                timeout=0.01,
                max_output_bytes=1_000,
                max_output_lines=100,
            )
        )
        await cleanup_started.wait()
        close_task = asyncio.create_task(supervisor.aclose())
        await anyio.sleep(0.05)
        assert max_active_cleanups == 1

        allow_cleanup.set()
        with pytest.raises(ToolError, match="timed out"):
            await run_task
        await close_task

    anyio.run(run)

    assert max_active_cleanups == 1


def test_cancelling_cancel_call_does_not_cancel_process_finalization(tmp_path: Path) -> None:
    async def run() -> ProcessUpdate:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(
                python_command("import time; time.sleep(30)"),
                cwd=tmp_path,
                timeout=30,
            )
            managed = supervisor._managed[process_id]  # noqa: SLF001
            assert managed.completion_task is not None
            managed.completion_task.cancel()
            await asyncio.gather(managed.completion_task, return_exceptions=True)

            finalization_started = asyncio.Event()
            allow_finalization = asyncio.Event()

            async def delayed_finalization() -> None:
                finalization_started.set()
                await allow_finalization.wait()
                await managed.process.wait()
                managed.exit_code = managed.process.returncode
                managed.state = managed.terminal_override or "completed"
                managed.changed.set()

            managed.completion_task = asyncio.create_task(delayed_finalization())
            cancel_call = asyncio.create_task(supervisor.cancel(process_id))
            await finalization_started.wait()
            cancel_call.cancel()
            await anyio.sleep(0)

            assert cancel_call.done() is False
            assert managed.operation_lock.locked() is True
            assert managed.completion_task.cancelled() is False
            allow_finalization.set()
            with pytest.raises(asyncio.CancelledError):
                await cancel_call
            await managed.completion_task
            return await supervisor.poll(process_id)
        finally:
            await supervisor.aclose()

    update = anyio.run(run)

    assert update.state == "cancelled"
    assert update.exit_code is None


def test_aclose_finishes_cleanup_before_propagating_caller_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_terminate = process_manager_module._terminate_process_tree  # type: ignore[attr-defined]
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()

    async def delayed_terminate(process: asyncio.subprocess.Process) -> bool:
        cleanup_started.set()
        await allow_cleanup.wait()
        return await original_terminate(process)

    monkeypatch.setattr(process_manager_module, "_terminate_process_tree", delayed_terminate)

    async def run() -> int:
        supervisor = ProcessSupervisor()
        process_id = await supervisor.start(
            python_command("import time; time.sleep(30)"),
            cwd=tmp_path,
            timeout=30,
        )
        process = supervisor._managed[process_id].process  # noqa: SLF001
        assert process.pid is not None

        close_call = asyncio.create_task(supervisor.aclose())
        await cleanup_started.wait()
        close_call.cancel()
        await anyio.sleep(0)
        assert close_call.done() is False

        close_call.cancel()
        await anyio.sleep(0)
        assert close_call.done() is False

        allow_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await close_call
        assert process.returncode is not None
        return process.pid

    pid = anyio.run(run)

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_aclose_serializes_cleanup_with_managed_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_terminate = process_manager_module._terminate_process_tree  # type: ignore[attr-defined]
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    active_cleanups = 0
    max_active_cleanups = 0

    async def delayed_terminate(process: asyncio.subprocess.Process) -> bool:
        nonlocal active_cleanups, max_active_cleanups
        active_cleanups += 1
        max_active_cleanups = max(max_active_cleanups, active_cleanups)
        cleanup_started.set()
        await allow_cleanup.wait()
        try:
            return await original_terminate(process)
        finally:
            active_cleanups -= 1

    monkeypatch.setattr(process_manager_module, "_terminate_process_tree", delayed_terminate)

    async def run() -> None:
        supervisor = ProcessSupervisor()
        process_id = await supervisor.start(
            python_command("import time; time.sleep(30)"),
            cwd=tmp_path,
            timeout=30,
        )
        managed = supervisor._managed[process_id]  # noqa: SLF001
        assert managed.completion_task is not None
        managed.completion_task.cancel()
        await asyncio.gather(managed.completion_task, return_exceptions=True)

        async def finalize() -> None:
            await managed.process.wait()
            managed.exit_code = managed.process.returncode
            managed.state = managed.terminal_override or "completed"
            managed.changed.set()

        managed.completion_task = asyncio.create_task(finalize())
        cancel_call = asyncio.create_task(supervisor.cancel(process_id))
        await cleanup_started.wait()
        close_call = asyncio.create_task(supervisor.aclose())
        await anyio.sleep(0.05)
        assert max_active_cleanups == 1

        allow_cleanup.set()
        await cancel_call
        await close_call

    anyio.run(run)

    assert max_active_cleanups == 1


def test_aclose_initializes_cleanup_inside_cancelled_scope_while_lock_is_contended(
    tmp_path: Path,
) -> None:
    async def run() -> int:
        supervisor = ProcessSupervisor()
        process_id = await supervisor.start(
            python_command("import time; time.sleep(30)"),
            cwd=tmp_path,
            timeout=30,
        )
        process = supervisor._managed[process_id].process  # noqa: SLF001
        assert process.pid is not None

        lock_held = anyio.Event()
        release_lock = anyio.Event()
        close_finished = anyio.Event()

        async def hold_lock() -> None:
            async with supervisor._lock:  # noqa: SLF001
                lock_held.set()
                await release_lock.wait()

        async def close_from_cancelled_scope() -> None:
            with anyio.CancelScope() as cancel_scope:
                cancel_scope.cancel()
                await supervisor.aclose()
            close_finished.set()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(hold_lock)
            await lock_held.wait()
            task_group.start_soon(close_from_cancelled_scope)
            await anyio.sleep(0.05)
            assert close_finished.is_set() is False
            release_lock.set()

        assert close_finished.is_set() is True
        assert process.returncode is not None
        return process.pid

    pid = anyio.run(run)

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_aclose_initializes_cleanup_after_raw_task_cancel_while_lock_is_contended(
    tmp_path: Path,
) -> None:
    async def run() -> int:
        supervisor = ProcessSupervisor()
        process_id = await supervisor.start(
            python_command("import time; time.sleep(30)"),
            cwd=tmp_path,
            timeout=30,
        )
        process = supervisor._managed[process_id].process  # noqa: SLF001
        assert process.pid is not None

        lock_held = asyncio.Event()
        release_lock = asyncio.Event()

        async def hold_lock() -> None:
            async with supervisor._lock:  # noqa: SLF001
                lock_held.set()
                await release_lock.wait()

        hold_lock_task = asyncio.create_task(hold_lock())
        await lock_held.wait()

        close_task = asyncio.create_task(supervisor.aclose())
        await anyio.sleep(0.05)
        close_task.cancel()
        await anyio.sleep(0.05)
        assert close_task.done() is False

        release_lock.set()
        await hold_lock_task
        with anyio.fail_after(3):
            with pytest.raises(asyncio.CancelledError):
                await close_task
        assert process.returncode is not None
        return process.pid

    pid = anyio.run(run)

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.production_fault
@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group assertion")
def test_managed_process_cancel_terminates_descendants(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    child_source = "import time; time.sleep(30)"
    parent_source = (
        "import pathlib,subprocess,sys,time;"
        f"child=subprocess.Popen([sys.executable,'-c',{child_source!r}]);"
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid));"
        "time.sleep(30)"
    )

    async def run() -> int:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(
                python_command(parent_source),
                cwd=tmp_path,
                timeout=30,
            )
            child_pid = await wait_for_pid_file(child_pid_path)
            update = await supervisor.cancel(process_id)
            assert update.state == "cancelled"
            return child_pid
        finally:
            await supervisor.aclose()

    child_pid = anyio.run(run)

    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


@pytest.mark.production_fault
@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group assertion")
def test_supervisor_close_terminates_abandoned_processes(tmp_path: Path) -> None:
    pid_path = tmp_path / "process.pid"
    source = (
        "import os,pathlib,time;"
        f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()));"
        "time.sleep(30)"
    )

    async def run() -> int:
        supervisor = ProcessSupervisor()
        await supervisor.start(
            python_command(source),
            cwd=tmp_path,
            timeout=30,
        )
        pid = await wait_for_pid_file(pid_path)
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(supervisor.aclose)
            task_group.start_soon(supervisor.aclose)
        await supervisor.aclose()
        return pid

    pid = anyio.run(run)

    with pytest.raises(ProcessLookupError):
        os.kill(pid, signal.SIGCONT)
