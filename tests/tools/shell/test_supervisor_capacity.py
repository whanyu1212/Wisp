from __future__ import annotations

import asyncio
from pathlib import Path

import anyio
import pytest

from tests.tools.shell.supervisor_support import (
    poll_until_terminal,
    python_command,
)
from wisp.tools.result import ToolError
from wisp.tools.shell.supervisor import (
    ProcessSupervisor,
    ProcessUpdate,
)

pytestmark = pytest.mark.process


def test_managed_process_limit_recovers_after_terminal_result_is_observed(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        supervisor = ProcessSupervisor(max_processes=1)
        try:
            first = await supervisor.start(
                python_command("import time; time.sleep(10)"),
                cwd=tmp_path,
                timeout=2,
            )
            with pytest.raises(ToolError, match="managed process limit"):
                await supervisor.start(
                    python_command("print('blocked')"),
                    cwd=tmp_path,
                    timeout=2,
                )
            cancelled = await supervisor.cancel(first)
            assert cancelled.state == "cancelled"

            second = await supervisor.start(
                python_command("print('accepted')"),
                cwd=tmp_path,
                timeout=2,
            )
            terminal = (await poll_until_terminal(supervisor, second))[-1]
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
                        python_command("import time; time.sleep(10)"),
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
                python_command("import time; time.sleep(30)"),
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


def test_unreported_terminal_handle_blocks_capacity_until_polled(tmp_path: Path) -> None:
    async def run() -> tuple[ProcessUpdate, tuple[ProcessUpdate, ...]]:
        supervisor = ProcessSupervisor(max_processes=1)
        try:
            first = await supervisor.start(
                python_command("print('unobserved')"),
                cwd=tmp_path,
                timeout=2,
            )
            with anyio.fail_after(3):
                while (
                    supervisor._managed[first].state == "running"  # noqa: SLF001
                    or not supervisor._managed[first].stdout.text  # noqa: SLF001
                ):
                    await anyio.sleep(0.01)

            with pytest.raises(ToolError, match="managed process limit"):
                await supervisor.start(
                    python_command("print('blocked')"),
                    cwd=tmp_path,
                    timeout=2,
                )

            first_update = await supervisor.poll(first)
            second = await supervisor.start(
                python_command("print('accepted')"),
                cwd=tmp_path,
                timeout=2,
            )
            return first_update, await poll_until_terminal(supervisor, second)
        finally:
            await supervisor.aclose()

    first_update, second_updates = anyio.run(run)

    assert first_update.state == "completed"
    assert first_update.stdout == "unobserved\n"
    assert second_updates[-1].state == "completed"
    assert "".join(update.stdout for update in second_updates) == "accepted\n"


def test_reported_terminal_handle_is_evicted_for_capacity(tmp_path: Path) -> None:
    async def run() -> tuple[ProcessUpdate, ...]:
        supervisor = ProcessSupervisor(max_processes=1)
        try:
            first = await supervisor.start(
                python_command("print('observed')"),
                cwd=tmp_path,
                timeout=2,
            )
            await poll_until_terminal(supervisor, first)
            with anyio.fail_after(3):
                while True:
                    try:
                        second = await supervisor.start(
                            python_command("print('accepted')"),
                            cwd=tmp_path,
                            timeout=2,
                        )
                    except ToolError:
                        await anyio.sleep(0.01)
                    else:
                        break
            return await poll_until_terminal(supervisor, second)
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
                python_command("import time; time.sleep(10)"),
                cwd=tmp_path,
                timeout=2,
            )
            with pytest.raises(ToolError, match="managed process limit"):
                await supervisor.run_to_completion(
                    python_command("print('blocked')"),
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
                python_command("print('managed')"),
                cwd=tmp_path,
                timeout=2,
            )
            await poll_until_terminal(supervisor, process_id)
            result = await supervisor.run_to_completion(
                python_command("print('one-shot')"),
                cwd=tmp_path,
                timeout=2,
                max_output_bytes=1_000,
                max_output_lines=100,
            )
            return result.stdout
        finally:
            await supervisor.aclose()

    assert anyio.run(run) == "one-shot\n"
