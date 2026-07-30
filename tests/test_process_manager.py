from __future__ import annotations

import asyncio
import os
import shlex
import signal
import sys
from pathlib import Path

import anyio
import pytest

import wisp.tools.process_manager as process_manager_module
from wisp.tools.process_manager import ProcessSupervisor, ProcessUpdate, _bounded_text_tail
from wisp.tools.result import ToolError


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


async def _poll_until_terminal(
    supervisor: ProcessSupervisor,
    process_id: str,
    *,
    timeout: float = 3,
) -> tuple[ProcessUpdate, ...]:
    updates: list[ProcessUpdate] = []
    with anyio.fail_after(timeout):
        while True:
            update = await supervisor.poll(process_id, wait_seconds=0.1)
            updates.append(update)
            if update.state != "running":
                return tuple(updates)


def test_managed_process_polling_delivers_incremental_output_once(tmp_path: Path) -> None:
    async def run() -> tuple[ProcessUpdate, ...]:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(
                _python_command(
                    "import sys,time;"
                    "print('first', flush=True);"
                    "time.sleep(0.15);"
                    "print('second', flush=True);"
                    "sys.stderr.write('warning\\n');"
                    "sys.stderr.flush()"
                ),
                cwd=tmp_path,
                timeout=2,
            )
            return await _poll_until_terminal(supervisor, process_id)
        finally:
            await supervisor.aclose()

    updates = anyio.run(run)

    assert "".join(update.stdout for update in updates) == "first\nsecond\n"
    assert "".join(update.stderr for update in updates) == "warning\n"
    assert updates[-1].state == "completed"
    assert updates[-1].exit_code == 0


def test_managed_process_retention_is_bounded_and_utf8_safe(tmp_path: Path) -> None:
    async def run() -> ProcessUpdate:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(
                _python_command("print('🙂' * 50); print('tail')"),
                cwd=tmp_path,
                timeout=2,
                max_retained_bytes=20,
                max_retained_lines=1,
            )
            await anyio.sleep(0.1)
            return await supervisor.poll(process_id)
        finally:
            await supervisor.aclose()

    update = anyio.run(run)

    assert update.state == "completed"
    assert len(update.stdout.encode("utf-8")) <= 20
    assert update.stdout.endswith("tail\n")
    assert "\ufffd" not in update.stdout
    assert update.stdout_truncated is True
    assert update.stdout_dropped_bytes > 0


def test_managed_process_reports_stream_reader_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = ProcessSupervisor()

    async def fail_read(*_args: object) -> None:
        raise OSError("broken transport")

    monkeypatch.setattr(supervisor, "_read_stream", fail_read)

    async def run() -> ProcessUpdate:
        try:
            process_id = await supervisor.start(
                _python_command("print('unread')"),
                cwd=tmp_path,
                timeout=2,
            )
            return (await _poll_until_terminal(supervisor, process_id))[-1]
        finally:
            await supervisor.aclose()

    update = anyio.run(run)

    assert update.state == "failed"
    assert update.exit_code is None
    assert update.error == "Failed to read process output"


def test_managed_process_reports_reader_failure_before_process_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = ProcessSupervisor()

    async def fail_read(*_args: object) -> None:
        raise OSError("broken transport")

    monkeypatch.setattr(supervisor, "_read_stream", fail_read)

    async def run() -> ProcessUpdate:
        try:
            process_id = await supervisor.start(
                _python_command("import time; time.sleep(30)"),
                cwd=tmp_path,
                timeout=60,
            )
            return (await _poll_until_terminal(supervisor, process_id, timeout=2))[-1]
        finally:
            await supervisor.aclose()

    update = anyio.run(run)

    assert update.state == "failed"
    assert update.exit_code is None
    assert update.error == "Failed to read process output"


@pytest.mark.parametrize("separator", ["\n", "\r", "\r\n", "\u2028"])
def test_retention_counts_unterminated_trailing_logical_line(separator: str) -> None:
    text = f"first{separator}second"

    bounded, dropped_bytes = _bounded_text_tail(
        text,
        max_bytes=1_000,
        max_lines=1,
    )

    assert bounded == "second"
    assert dropped_bytes == len(f"first{separator}".encode())


def test_managed_process_timeout_is_not_an_exit_code(tmp_path: Path) -> None:
    async def run() -> ProcessUpdate:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(
                _python_command("import time; time.sleep(10)"),
                cwd=tmp_path,
                timeout=0.05,
            )
            return (await _poll_until_terminal(supervisor, process_id))[-1]
        finally:
            await supervisor.aclose()

    update = anyio.run(run)

    assert update.state == "timed_out"
    assert update.exit_code is None


def test_managed_timeout_bounds_post_termination_stream_drain(tmp_path: Path) -> None:
    async def hold_pipe_open() -> None:
        await asyncio.Event().wait()

    async def run() -> tuple[ProcessUpdate, tuple[asyncio.Task[None], ...]]:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(
                _python_command("import time; time.sleep(30)"),
                cwd=tmp_path,
                timeout=0.05,
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
                update = (await _poll_until_terminal(supervisor, process_id))[-1]
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
            _python_command("import time; time.sleep(30)"),
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
            with anyio.fail_after(3):
                while not child_pid_path.exists():
                    await anyio.sleep(0.01)
            child_pid = int(child_pid_path.read_text())
            terminal = (await _poll_until_terminal(supervisor, process_id))[-1]
            return terminal, child_pid
        finally:
            await supervisor.aclose()

    update, child_pid = anyio.run(run)

    assert update.state == "timed_out"
    assert update.exit_code is None
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


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
        with anyio.fail_after(3):
            while not child_pid_path.exists():
                await anyio.sleep(0.01)
        child_pid = int(child_pid_path.read_text())
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
            with anyio.fail_after(3):
                while not child_pid_path.exists():
                    await anyio.sleep(0.01)
            child_pid = int(child_pid_path.read_text())
            terminal = (await _poll_until_terminal(supervisor, process_id))[-1]
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


def test_managed_process_limit_recovers_after_terminal_result_is_observed(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        supervisor = ProcessSupervisor(max_processes=1)
        try:
            first = await supervisor.start(
                _python_command("import time; time.sleep(10)"),
                cwd=tmp_path,
                timeout=2,
            )
            with pytest.raises(ToolError, match="managed process limit"):
                await supervisor.start(
                    _python_command("print('blocked')"),
                    cwd=tmp_path,
                    timeout=2,
                )
            cancelled = await supervisor.cancel(first)
            assert cancelled.state == "cancelled"

            second = await supervisor.start(
                _python_command("print('accepted')"),
                cwd=tmp_path,
                timeout=2,
            )
            terminal = (await _poll_until_terminal(supervisor, second))[-1]
            assert terminal.state == "completed"
        finally:
            await supervisor.aclose()

    anyio.run(run)


def test_concurrent_starts_cannot_exceed_managed_process_limit(tmp_path: Path) -> None:
    async def run() -> tuple[int, int]:
        supervisor = ProcessSupervisor(max_processes=1)
        started: list[str] = []
        rejected = 0

        async def start_one() -> None:
            nonlocal rejected
            try:
                started.append(
                    await supervisor.start(
                        _python_command("import time; time.sleep(10)"),
                        cwd=tmp_path,
                        timeout=2,
                    )
                )
            except ToolError as exc:
                assert "managed process limit" in str(exc)
                rejected += 1

        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(start_one)
                task_group.start_soon(start_one)
            assert len(started) == 1
            await supervisor.cancel(started[0])
            return len(started), rejected
        finally:
            await supervisor.aclose()

    assert anyio.run(run) == (1, 1)


def test_poll_wait_releases_operation_lock_for_cancel(tmp_path: Path) -> None:
    class InstrumentedChange:
        def __init__(self) -> None:
            self._event = asyncio.Event()
            self.wait_started = asyncio.Event()

        def clear(self) -> None:
            self._event.clear()

        def set(self) -> None:
            self._event.set()

        async def wait(self) -> None:
            self.wait_started.set()
            await self._event.wait()

    async def run() -> tuple[ProcessUpdate, ProcessUpdate]:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(
                _python_command("import time; time.sleep(30)"),
                cwd=tmp_path,
                timeout=60,
            )
            managed = supervisor._managed[process_id]  # noqa: SLF001
            changed = InstrumentedChange()
            managed.changed = changed  # type: ignore[assignment]

            poll_task = asyncio.create_task(supervisor.poll(process_id, wait_seconds=30))
            await changed.wait_started.wait()

            with anyio.fail_after(2):
                cancel_update = await supervisor.cancel(process_id)
            with anyio.fail_after(2):
                poll_update = await poll_task
            return cancel_update, poll_update
        finally:
            await supervisor.aclose()

    cancel_update, poll_update = anyio.run(run)

    assert cancel_update.state == "cancelled"
    assert poll_update.state == "cancelled"


def test_unreported_terminal_handle_is_evicted_for_capacity(tmp_path: Path) -> None:
    async def run() -> tuple[ProcessUpdate, ...]:
        supervisor = ProcessSupervisor(max_processes=1)
        try:
            await supervisor.start(
                _python_command("print('unobserved')"),
                cwd=tmp_path,
                timeout=2,
            )
            with anyio.fail_after(3):
                while True:
                    try:
                        second = await supervisor.start(
                            _python_command("print('accepted')"),
                            cwd=tmp_path,
                            timeout=2,
                        )
                    except ToolError:
                        await anyio.sleep(0.01)
                    else:
                        break
            return await _poll_until_terminal(supervisor, second)
        finally:
            await supervisor.aclose()

    updates = anyio.run(run)

    assert updates[-1].state == "completed"
    assert "".join(update.stdout for update in updates) == "accepted\n"


def test_one_shot_commands_share_managed_process_capacity(tmp_path: Path) -> None:
    async def run() -> None:
        supervisor = ProcessSupervisor(max_processes=1)
        try:
            process_id = await supervisor.start(
                _python_command("import time; time.sleep(10)"),
                cwd=tmp_path,
                timeout=2,
            )
            with pytest.raises(ToolError, match="managed process limit"):
                await supervisor.run_to_completion(
                    _python_command("print('blocked')"),
                    cwd=tmp_path,
                    timeout=2,
                    max_output_bytes=1_000,
                    max_output_lines=100,
                )
            await supervisor.cancel(process_id)
        finally:
            await supervisor.aclose()

    anyio.run(run)


def test_one_shot_evicts_observed_terminal_handle_for_capacity(tmp_path: Path) -> None:
    async def run() -> str:
        supervisor = ProcessSupervisor(max_processes=1)
        try:
            process_id = await supervisor.start(
                _python_command("print('managed')"),
                cwd=tmp_path,
                timeout=2,
            )
            await _poll_until_terminal(supervisor, process_id)
            result = await supervisor.run_to_completion(
                _python_command("print('one-shot')"),
                cwd=tmp_path,
                timeout=2,
                max_output_bytes=1_000,
                max_output_lines=100,
            )
            return result.stdout
        finally:
            await supervisor.aclose()

    assert anyio.run(run) == "one-shot\n"


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
            child_pid = int(child_pid_path.read_text())
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


def test_one_shot_releases_ownership_inside_cancelled_scope_while_lock_is_contended(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_terminate = process_manager_module._terminate_process_tree  # type: ignore[attr-defined]
    terminate_started = anyio.Event()
    allow_terminate = anyio.Event()

    async def delayed_terminate(process: asyncio.subprocess.Process) -> None:
        terminate_started.set()
        await allow_terminate.wait()
        await original_terminate(process)

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
                    _python_command("print('first')"),
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
                _python_command("print('second')"),
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

    async def delayed_terminate(process: asyncio.subprocess.Process) -> None:
        terminate_started.set()
        await allow_terminate.wait()
        await original_terminate(process)

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
                    _python_command("print('first')"),
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
                _python_command("print('second')"),
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
                    _python_command("import time; time.sleep(30)"),
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


def test_polling_terminal_process_does_not_repeat_output(tmp_path: Path) -> None:
    async def run() -> tuple[tuple[ProcessUpdate, ...], ProcessUpdate]:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(
                _python_command("print('once')"),
                cwd=tmp_path,
                timeout=2,
            )
            updates = await _poll_until_terminal(supervisor, process_id)
            repeated = await supervisor.poll(process_id)
            return updates, repeated
        finally:
            await supervisor.aclose()

    updates, repeated = anyio.run(run)

    assert "".join(update.stdout for update in updates) == "once\n"
    assert repeated.state == "completed"
    assert repeated.stdout == ""
    assert repeated.stderr == ""


def test_closed_and_unknown_process_handles_fail_deterministically(tmp_path: Path) -> None:
    async def run() -> None:
        supervisor = ProcessSupervisor()
        with pytest.raises(ToolError, match="Unknown managed process: missing"):
            await supervisor.poll("missing")
        await supervisor.aclose()
        with pytest.raises(RuntimeError, match="ProcessSupervisor is closed"):
            await supervisor.start(
                _python_command("print('no')"),
                cwd=tmp_path,
                timeout=2,
            )

    anyio.run(run)


def test_cancelling_cancel_call_does_not_cancel_process_finalization(tmp_path: Path) -> None:
    async def run() -> ProcessUpdate:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(
                _python_command("import time; time.sleep(30)"),
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
            with pytest.raises(asyncio.CancelledError):
                await cancel_call

            assert managed.completion_task.cancelled() is False
            allow_finalization.set()
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

    async def delayed_terminate(process: asyncio.subprocess.Process) -> None:
        cleanup_started.set()
        await allow_cleanup.wait()
        await original_terminate(process)

    monkeypatch.setattr(process_manager_module, "_terminate_process_tree", delayed_terminate)

    async def run() -> int:
        supervisor = ProcessSupervisor()
        process_id = await supervisor.start(
            _python_command("import time; time.sleep(30)"),
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


def test_aclose_initializes_cleanup_inside_cancelled_scope_while_lock_is_contended(
    tmp_path: Path,
) -> None:
    async def run() -> int:
        supervisor = ProcessSupervisor()
        process_id = await supervisor.start(
            _python_command("import time; time.sleep(30)"),
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
            _python_command("import time; time.sleep(30)"),
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
                _python_command(parent_source),
                cwd=tmp_path,
                timeout=30,
            )
            with anyio.fail_after(3):
                while not child_pid_path.exists():
                    await anyio.sleep(0.01)
            child_pid = int(child_pid_path.read_text())
            update = await supervisor.cancel(process_id)
            assert update.state == "cancelled"
            return child_pid
        finally:
            await supervisor.aclose()

    child_pid = anyio.run(run)

    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


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
            _python_command(source),
            cwd=tmp_path,
            timeout=30,
        )
        with anyio.fail_after(3):
            while not pid_path.exists():
                await anyio.sleep(0.01)
        pid = int(pid_path.read_text())
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(supervisor.aclose)
            task_group.start_soon(supervisor.aclose)
        await supervisor.aclose()
        return pid

    pid = anyio.run(run)

    with pytest.raises(ProcessLookupError):
        os.kill(pid, signal.SIGCONT)
