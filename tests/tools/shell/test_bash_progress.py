"""A running one-shot bash command reports its output so far."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

import anyio
import pytest

from tests.tools.shell.supervisor_support import python_command
from wisp.tools.context import ToolContext
from wisp.tools.output_store import OutputStore
from wisp.tools.progress import OutputProgress
from wisp.tools.result import ToolError
from wisp.tools.shell.supervisor import ProcessSupervisor
from wisp.tools.shell.tool import BashTool

pytestmark = pytest.mark.process

_TICKING = python_command(
    "import sys, time\n"
    "for n in range(8):\n"
    "    print(f'tick {n}', flush=True)\n"
    "    print(f'warn {n}', file=sys.stderr, flush=True)\n"
    "    time.sleep(0.1)\n"
)


class RecordingProgress:
    def __init__(self) -> None:
        self.reports: list[OutputProgress] = []

    def report(self, progress: OutputProgress) -> None:
        self.reports.append(progress)


def run_bash[T](
    tmp_path: Path, scenario: Callable[[BashTool, ToolContext, RecordingProgress], Awaitable[T]]
) -> tuple[T, RecordingProgress]:
    progress = RecordingProgress()

    async def run() -> T:
        supervisor = ProcessSupervisor(
            max_processes=1, output_store=OutputStore(tmp_path / "tool-output")
        )
        bash = BashTool(supervisor)
        context = replace(ToolContext(cwd=tmp_path), progress=progress)
        try:
            return await scenario(bash, context, progress)
        finally:
            await bash.aclose()

    return anyio.run(run), progress


def test_running_command_reports_growing_output_from_both_streams(tmp_path: Path) -> None:
    async def scenario(bash: BashTool, context: ToolContext, _: RecordingProgress) -> str:
        return (await bash.run({"command": _TICKING}, context)).text

    result, progress = run_bash(tmp_path, scenario)

    assert result.startswith("Command exited with code 0")
    assert len(progress.reports) >= 2
    totals = [(report.stdout_bytes, report.stderr_bytes) for report in progress.reports]
    assert totals == sorted(totals)
    assert len(set(totals)) == len(totals)  # reported only when output grew
    assert "tick" in progress.reports[-1].stdout_tail
    assert "warn" in progress.reports[-1].stderr_tail


def test_quiet_command_reports_nothing(tmp_path: Path) -> None:
    async def scenario(bash: BashTool, context: ToolContext, _: RecordingProgress) -> None:
        await bash.run({"command": python_command("import time; time.sleep(0.6)")}, context)

    _, progress = run_bash(tmp_path, scenario)

    assert progress.reports == []


@pytest.mark.parametrize("timeout", [None, 1], ids=["completed", "timed-out"])
def test_reporting_stops_when_the_call_ends(tmp_path: Path, timeout: int | None) -> None:
    command = python_command(
        "import time\nwhile True:\n    print('busy', flush=True)\n    time.sleep(0.05)\n"
        if timeout
        else "import time\nfor _ in range(6):\n    print('busy', flush=True)\n    time.sleep(0.1)\n"
    )

    async def scenario(
        bash: BashTool, context: ToolContext, progress: RecordingProgress
    ) -> tuple[int, int]:
        arguments: dict[str, object] = {"command": command}
        if timeout:
            arguments["timeout"] = timeout
            with pytest.raises(ToolError, match="timed out"):
                await bash.run(arguments, context)
        else:
            await bash.run(arguments, context)
        at_end = len(progress.reports)
        await anyio.sleep(0.6)
        return at_end, len(progress.reports)

    (at_end, later), _ = run_bash(tmp_path, scenario)

    assert at_end >= 1
    assert later == at_end


def test_scope_cancellation_with_progress_still_cleans_up(tmp_path: Path) -> None:
    # anyio cancellation stays in effect, so any await left in the supervisor's
    # cleanup raises again; the slot and saved output must still be released.
    command = python_command("import time\nprint('x' * 5000, flush=True)\ntime.sleep(30)\n")

    async def scenario(
        bash: BashTool, context: ToolContext, progress: RecordingProgress
    ) -> tuple[list[Path], str]:
        with anyio.CancelScope() as scope:

            async def cancel_once_reported() -> None:
                with anyio.fail_after(10):
                    while not progress.reports:
                        await anyio.sleep(0.01)
                scope.cancel()

            async with anyio.create_task_group() as task_group:
                task_group.start_soon(cancel_once_reported)
                await bash.run({"command": command, "timeout": 30}, context)
        saved = sorted((tmp_path / "tool-output").glob("runtime-*/*.log"))
        # The supervisor has one slot; a second command runs only if it was released.
        after = await bash.run({"command": python_command("print('next')")}, context)
        return saved, after.text

    (saved, after), _ = run_bash(tmp_path, scenario)

    assert saved == []
    assert after == "Command exited with code 0: next"
