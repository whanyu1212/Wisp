"""Bash results that truncate output save it in full for read_output."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

import anyio
import pytest

from tests.tools.shell.supervisor_support import python_command
from wisp.tools.context import ToolContext
from wisp.tools.output_store import OutputStore
from wisp.tools.read_output import ReadOutputTool
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.shell.supervisor import ProcessSupervisor
from wisp.tools.shell.tool import BashTool

pytestmark = pytest.mark.process

_NOISY_COMMAND = python_command(
    "print('EARLY ERROR: config missing')\n"
    "print('\\n'.join(f'progress {n}' for n in range(3000)))\n"
    "print('final summary')\n"
)
_OUTPUT_ID = re.compile(r"(stdout|stderr) output_id=([0-9a-f]+)")


class Shell:
    """A bash tool and read_output sharing one supervisor and store."""

    def __init__(self, root: Path, *, max_processes: int = 2) -> None:
        self.root = root
        self.supervisor = ProcessSupervisor(
            max_processes=max_processes, output_store=OutputStore(root / "tool-output")
        )
        self.bash = BashTool(self.supervisor)
        self.read_output = ReadOutputTool(self.supervisor.output_store)
        self.context = ToolContext(cwd=root, max_output_bytes=400, max_output_lines=20)

    async def run(self, arguments: dict[str, object]) -> ToolResult:
        return await self.bash.run(arguments, self.context)

    async def read(self, output_id: str, **paging: int) -> str:
        context = ToolContext(cwd=self.root)
        return (await self.read_output.run({"output_id": output_id, **paging}, context)).text

    def saved_files(self) -> list[Path]:
        return sorted((self.root / "tool-output").glob("runtime-*/*.log"))


def output_ids(text: str) -> dict[str, str]:
    return dict(_OUTPUT_ID.findall(text))


def with_shell[T](tmp_path: Path, scenario: Callable[[Shell], Awaitable[T]]) -> T:
    async def run() -> T:
        shell = Shell(tmp_path)
        try:
            return await scenario(shell)
        finally:
            await shell.bash.aclose()

    return anyio.run(run)


def test_tail_only_run_saves_the_early_error_it_cut(tmp_path: Path) -> None:
    async def scenario(shell: Shell) -> tuple[ToolResult, str, str]:
        result = await shell.run({"command": _NOISY_COMMAND})
        stdout_id = output_ids(result.text)["stdout"]
        first_page = await shell.read(stdout_id, limit=2)
        last_page = await shell.read(stdout_id, offset=3001)
        return result, first_page, last_page

    result, first_page, last_page = with_shell(tmp_path, scenario)

    assert result.truncated is True
    assert "EARLY ERROR" not in result.text
    assert result.text.endswith("Read it with read_output.]")
    assert result.data["stdout_output_id"] == output_ids(result.text)["stdout"]
    assert first_page.splitlines()[1:3] == ["EARLY ERROR: config missing", "progress 0"]
    assert last_page.splitlines()[1:] == ["progress 2999", "final summary"]


def test_output_cut_only_by_the_line_limit_is_saved(tmp_path: Path) -> None:
    command = python_command("print('\\n'.join(str(n) for n in range(50)))")

    async def scenario(shell: Shell) -> tuple[ToolResult, str]:
        result = await shell.run({"command": command})
        return result, await shell.read(output_ids(result.text)["stdout"], limit=1)

    result, first_page = with_shell(tmp_path, scenario)

    header, first_line, _continuation = first_page.splitlines()
    assert result.data["stdout_dropped_bytes"] > 0
    assert header.startswith("[stdout of `")
    assert header.endswith(": lines 1-1 of 50]")
    assert first_line == "0"


def test_complete_output_saves_nothing(tmp_path: Path) -> None:
    async def scenario(shell: Shell) -> tuple[ToolResult, list[Path]]:
        result = await shell.run({"command": python_command("print('fits')")})
        return result, shell.saved_files()

    result, files = with_shell(tmp_path, scenario)

    assert result.text == "Command exited with code 0: fits"
    assert "stdout_output_id" not in result.data
    assert files == []
    assert not (tmp_path / "tool-output").exists()


def test_timeout_reports_saved_output_and_its_tail(tmp_path: Path) -> None:
    command = python_command("import time\nprint('connecting to db', flush=True)\ntime.sleep(30)\n")

    async def scenario(shell: Shell) -> tuple[str, str]:
        with pytest.raises(ToolError) as caught:
            await shell.run({"command": command, "timeout": 1})
        message = str(caught.value)
        return message, await shell.read(output_ids(message)["stdout"])

    message, saved_page = with_shell(tmp_path, scenario)

    lines = message.splitlines()
    assert lines[0] == "Command timed out after 1 seconds."
    assert lines[1].startswith("[Full output saved: stdout output_id=")
    assert lines[2:] == ["Last output:", "connecting to db"]
    assert saved_page.splitlines()[1:] == ["connecting to db"]


def test_cancelled_run_leaves_no_saved_files(tmp_path: Path) -> None:
    command = python_command("import time\nprint('x' * 5000, flush=True)\ntime.sleep(30)\n")

    async def scenario(shell: Shell) -> list[Path]:
        task = asyncio.create_task(shell.run({"command": command, "timeout": 30}))
        with anyio.fail_after(10):
            while not shell.saved_files():
                await anyio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return shell.saved_files()

    assert with_shell(tmp_path, scenario) == []


def test_slow_poll_can_read_output_dropped_between_polls(tmp_path: Path) -> None:
    async def scenario(shell: Shell) -> tuple[list[ToolResult], str]:
        started = await shell.run(
            {"operation": "start", "command": _NOISY_COMMAND, "yield_seconds": 0}
        )
        process_id = started.data["process_id"]
        results = [started]
        with anyio.fail_after(10):
            while results[-1].data["process_state"] == "running":
                # Polling late lets output pile up past what a poll retains.
                await anyio.sleep(0.5)
                results.append(await shell.run({"operation": "poll", "process_id": process_id}))
        stdout_ids = {
            result.data["stdout_output_id"]
            for result in results
            if "stdout_output_id" in result.data
        }
        assert len(stdout_ids) == 1
        return results, await shell.read(str(stdout_ids.pop()), limit=1)

    results, first_page = with_shell(tmp_path, scenario)

    assert any(result.data["stdout_dropped_bytes"] for result in results)
    assert first_page.splitlines()[1] == "EARLY ERROR: config missing"


def test_concurrent_commands_save_separate_output(tmp_path: Path) -> None:
    commands = [
        python_command(f"print('\\n'.join('{name} ' + str(n) for n in range(500)))")
        for name in ("alpha", "beta")
    ]

    async def scenario(shell: Shell) -> list[str]:
        results = await asyncio.gather(*(shell.run({"command": command}) for command in commands))
        return [await shell.read(output_ids(result.text)["stdout"], limit=1) for result in results]

    first_pages = with_shell(tmp_path, scenario)

    assert [page.splitlines()[1] for page in first_pages] == ["alpha 0", "beta 0"]


def test_closing_the_tool_deletes_saved_output(tmp_path: Path) -> None:
    async def scenario() -> list[Path]:
        shell = Shell(tmp_path)
        await shell.run({"command": _NOISY_COMMAND})
        assert shell.saved_files()
        await shell.bash.aclose()
        return shell.saved_files()

    assert anyio.run(scenario) == []
