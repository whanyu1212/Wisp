from __future__ import annotations

import asyncio
import os
import shlex
import sys
import time
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from tests.tools.support import run_tool
from wisp.tools.builtin import (
    BashTool,
    ProcessResult,
)
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.shell import process as process_tools_module
from wisp.tools.shell import supervisor as process_manager_module
from wisp.tools.shell import tool as shell_tools_module

pytestmark = pytest.mark.process


def test_process_result_preserves_public_positional_stdout_count_slot() -> None:
    result = ProcessResult(0, "out", "err", False, False, 7)

    assert result.stdout_count == 7
    assert result.stdout_dropped_bytes == 0
    assert result.stderr_dropped_bytes == 0


def test_bash_tool_schema_requires_operation_specific_arguments() -> None:
    schema = BashTool.input_schema

    assert schema["oneOf"] == [
        {
            "properties": {"operation": {"enum": ["run"]}},
            "required": ["command"],
        },
        {
            "properties": {"operation": {"enum": ["start"]}},
            "required": ["operation", "command"],
        },
        {
            "properties": {"operation": {"enum": ["poll"]}},
            "required": ["operation", "process_id"],
        },
        {
            "properties": {"operation": {"enum": ["cancel"]}},
            "required": ["operation", "process_id"],
        },
    ]


def test_bash_tool_captures_stdout_stderr_and_exit_code(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)
    python = shlex.quote(sys.executable)
    command = (
        f"{python} -c \"import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)\""
    )

    result = run_tool(BashTool(), {"command": command}, context)

    assert result.data["exit_code"] == 3
    assert result.data["stdout"] == "out\n"
    assert result.data["stderr"] == "err\n"
    assert result.text == "Command exited with code 3: out\nerr"


def test_bash_tool_reports_successful_exit_code_with_output(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)
    python = shlex.quote(sys.executable)
    command = f"{python} -c \"print('verified')\""

    result = run_tool(BashTool(), {"command": command}, context)

    assert result.text == "Command exited with code 0: verified"
    assert result.data["exit_code"] == 0


def test_bash_tool_reports_successful_exit_code_without_output(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)
    python = shlex.quote(sys.executable)
    command = f'{python} -c "pass"'

    result = run_tool(BashTool(), {"command": command}, context)

    assert result.text == "Command exited with code 0"
    assert result.data["exit_code"] == 0


def test_bash_tool_starts_polls_and_completes_resumable_process(tmp_path: Path) -> None:
    async def run() -> None:
        context = ToolContext(cwd=tmp_path)
        tool = BashTool()
        python = shlex.quote(sys.executable)
        code = (
            "import time; print('first', flush=True); time.sleep(0.2); print('second', flush=True)"
        )
        command = f"{python} -u -c {shlex.quote(code)}"

        try:
            start = await tool.run(
                {
                    "operation": "start",
                    "command": command,
                    "yield_seconds": 0,
                    "lifetime_seconds": 5,
                },
                context,
            )
            process_id = str(start.data["process_id"])
            chunks = [str(start.data["stdout"])]
            final: ToolResult | None = start if start.data["process_state"] == "completed" else None

            for _ in range(20):
                if final is not None:
                    break
                poll = await tool.run(
                    {
                        "operation": "poll",
                        "process_id": process_id,
                        "wait_seconds": 0.2,
                    },
                    context,
                )
                chunks.append(str(poll.data["stdout"]))
                if poll.data["process_state"] == "completed":
                    final = poll

            assert final is not None
            assert final.text.startswith(f"Process {process_id} completed with exit code 0")
            assert final.data["process_id"] == process_id
            assert final.data["process_state"] == "completed"
            assert final.data["exit_code"] == 0
            assert final.data["output_has_exit_status"] is False
            assert final.data["stdout_truncated"] is False
            assert final.data["stderr_truncated"] is False
            combined = "".join(chunks)
            assert combined.count("first\n") == 1
            assert combined.count("second\n") == 1
        finally:
            await tool.aclose()

    anyio.run(run)


def test_bash_managed_update_preserves_retained_stdout_after_label(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path, max_output_bytes=5, max_output_lines=1)
    update = process_manager_module.ProcessUpdate(
        process_id="p123",
        state="running",
        stdout="tail\n",
    )

    result = shell_tools_module._managed_update_result(update, context=context)

    assert result.text == "Process p123 is still running\nstdout:\ntail\n"
    assert result.data["stdout"] == "tail\n"
    assert result.truncated is False


def test_bash_managed_update_adds_poll_time_dropped_bytes(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path, max_output_bytes=20, max_output_lines=100)
    update = process_manager_module.ProcessUpdate(
        process_id="p123",
        state="running",
        stdout="x" * 100,
        stdout_dropped_bytes=3,
    )

    result = shell_tools_module._managed_update_result(update, context=context)
    stdout = str(result.data["stdout"])

    assert result.data["stdout_truncated"] is True
    retained_tail_bytes = len(stdout.removeprefix("[truncated] ").encode("utf-8"))
    assert (
        result.data["stdout_dropped_bytes"]
        == 3 + len(update.stdout.encode("utf-8")) - retained_tail_bytes
    )
    assert result.truncated is True


def test_bash_managed_update_counts_marker_only_output_as_dropped_bytes(
    tmp_path: Path,
) -> None:
    context = ToolContext(cwd=tmp_path, max_output_bytes=5, max_output_lines=0)
    update = process_manager_module.ProcessUpdate(
        process_id="p123",
        state="running",
        stdout="abcde",
    )

    result = shell_tools_module._managed_update_result(update, context=context)

    assert result.data["stdout"] == "[trun"
    assert result.data["stdout_truncated"] is True
    assert result.data["stdout_dropped_bytes"] == len(update.stdout.encode("utf-8"))
    assert result.truncated is True


def test_bash_tool_cancels_resumable_process(tmp_path: Path) -> None:
    async def run() -> None:
        context = ToolContext(cwd=tmp_path)
        tool = BashTool()
        python = shlex.quote(sys.executable)
        command = f'{python} -c "import time; time.sleep(30)"'

        try:
            start = await tool.run(
                {
                    "operation": "start",
                    "command": command,
                    "yield_seconds": 0,
                    "lifetime_seconds": 30,
                },
                context,
            )
            process_id = str(start.data["process_id"])

            cancelled = await tool.run(
                {"operation": "cancel", "process_id": process_id},
                context,
            )

            assert cancelled.text == f"Process {process_id} cancelled"
            assert cancelled.data["process_id"] == process_id
            assert cancelled.data["process_state"] == "cancelled"
            assert "exit_code" not in cancelled.data
        finally:
            await tool.aclose()

    anyio.run(run)


def test_bash_tool_cancels_started_process_when_initial_poll_is_cancelled(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        context = ToolContext(cwd=tmp_path)
        tool = BashTool()
        python = shlex.quote(sys.executable)
        command = f'{python} -c "import time; time.sleep(30)"'

        try:
            start_task = asyncio.create_task(
                tool.run(
                    {
                        "operation": "start",
                        "command": command,
                        "yield_seconds": 30,
                        "lifetime_seconds": 60,
                    },
                    context,
                )
            )
            supervisor = tool._process_supervisor  # noqa: SLF001
            assert supervisor is not None
            with anyio.fail_after(5):
                while not supervisor._managed:  # noqa: SLF001
                    await asyncio.sleep(0.01)
            process_id = next(iter(supervisor._managed))  # noqa: SLF001

            start_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await start_task

            update = await supervisor.poll(process_id)
            assert update.state == "cancelled"
        finally:
            await tool.aclose()

    anyio.run(run)


def test_bash_tool_shields_initial_poll_cleanup_under_anyio_cancellation(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        context = ToolContext(cwd=tmp_path)
        tool = BashTool()
        python = shlex.quote(sys.executable)
        command = f'{python} -c "import time; time.sleep(30)"'

        try:
            supervisor = tool._process_supervisor  # noqa: SLF001
            assert supervisor is not None

            async def start() -> None:
                await tool.run(
                    {
                        "operation": "start",
                        "command": command,
                        "yield_seconds": 30,
                        "lifetime_seconds": 60,
                    },
                    context,
                )

            async with anyio.create_task_group() as task_group:
                task_group.start_soon(start)
                with anyio.fail_after(5):
                    while not supervisor._managed:  # noqa: SLF001
                        await anyio.sleep(0.01)
                process_id = next(iter(supervisor._managed))  # noqa: SLF001
                task_group.cancel_scope.cancel()

            update = await supervisor.poll(process_id)
            assert update.state == "cancelled"
        finally:
            await tool.aclose()

    anyio.run(run)


def test_bash_tool_reports_resumable_timeout_as_terminal_state(tmp_path: Path) -> None:
    async def run() -> None:
        context = ToolContext(cwd=tmp_path)
        tool = BashTool()
        python = shlex.quote(sys.executable)
        command = f'{python} -c "import time; time.sleep(5)"'

        try:
            result = await tool.run(
                {
                    "operation": "start",
                    "command": command,
                    "yield_seconds": 1,
                    "lifetime_seconds": 0.1,
                },
                context,
            )
            process_id = str(result.data["process_id"])
            for _ in range(20):
                if result.data["process_state"] == "timed_out":
                    break
                result = await tool.run(
                    {
                        "operation": "poll",
                        "process_id": process_id,
                        "wait_seconds": 0.1,
                    },
                    context,
                )

            assert result.text.startswith(f"Process {process_id} timed out")
            assert result.data["process_state"] == "timed_out"
            assert result.data["output_has_exit_status"] is False
            assert "exit_code" not in result.data
        finally:
            await tool.aclose()

    anyio.run(run)


def test_bash_tool_requires_supervisor_for_resumable_operations(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)

    with pytest.raises(ToolError, match="bash.operation=poll requires a process supervisor"):
        run_tool(BashTool(None), {"operation": "poll", "process_id": "p1"}, context)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"operation": "bogus"}, "bash.operation must be one of"),
        (
            {"operation": "start", "command": "pwd", "yield_seconds": -1},
            "bash.yield_seconds must be greater than or equal to zero",
        ),
        (
            {"operation": "poll", "process_id": "p1", "wait_seconds": float("inf")},
            "bash.wait_seconds must be finite",
        ),
    ],
)
def test_bash_tool_validates_resumable_arguments(
    tmp_path: Path,
    arguments: dict[str, object],
    message: str,
) -> None:
    context = ToolContext(cwd=tmp_path)

    with pytest.raises(ToolError, match=message):
        run_tool(BashTool(), arguments, context)


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell signal assertion")
@pytest.mark.parametrize(("signal_name", "exit_code"), [("HUP", 129), ("INT", 130), ("TERM", 143)])
def test_bash_tool_preserves_posix_shell_signal_exit(
    tmp_path: Path,
    signal_name: str,
    exit_code: int,
) -> None:
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        BashTool(),
        {"command": f"kill -{signal_name} $$; echo survived"},
        context,
    )

    assert result.data["exit_code"] == exit_code
    assert result.data["stdout"] == ""


def test_bash_tool_preserves_exit_code_outside_tiny_body_budget(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def fake_run_shell(*args: object, **kwargs: object) -> process_tools_module.ProcessResult:
        return process_tools_module.ProcessResult(exit_code=7, stdout="", stderr="")

    monkeypatch.setattr(shell_tools_module, "_run_shell", fake_run_shell)
    context = ToolContext(cwd=tmp_path, max_output_bytes=1, max_output_lines=0)

    result = run_tool(BashTool(None), {"command": "ignored"}, context)

    assert result.text == "Command exited with code 7"
    assert result.data["output_has_exit_status"] is True
    assert result.truncated is False


def test_bash_tool_does_not_add_separator_for_newline_only_output(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)
    python = shlex.quote(sys.executable)
    command = f'{python} -c "print()"'

    result = run_tool(BashTool(), {"command": command}, context)

    assert result.text == "Command exited with code 0"
    assert result.data["stdout"] == "\n"


def test_bash_tool_retruncates_combined_stdout_and_stderr(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path, max_output_bytes=40, max_output_lines=100)
    python = shlex.quote(sys.executable)
    code = "import sys; sys.stdout.write('o' * 100); sys.stderr.write('e' * 100)"
    command = f"{python} -c {shlex.quote(code)}"

    result = run_tool(BashTool(), {"command": command}, context)

    status_overhead = len(f"Command exited with code {result.data['exit_code']}: ".encode())
    assert len(result.text.encode("utf-8")) <= context.max_output_bytes + status_overhead
    assert result.text.startswith(f"Command exited with code {result.data['exit_code']}:")
    assert result.truncated is True


def test_bash_tool_reserves_status_space_without_losing_diagnostic_tail(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def fake_run_shell(*args: object, **kwargs: object) -> process_tools_module.ProcessResult:
        return process_tools_module.ProcessResult(
            exit_code=2,
            stdout="setup output " * 8,
            stderr="traceback final diagnostic 尾",
            stdout_truncated=True,
        )

    monkeypatch.setattr(shell_tools_module, "_run_shell", fake_run_shell)
    context = ToolContext(cwd=tmp_path, max_output_bytes=80, max_output_lines=100)

    result = run_tool(BashTool(None), {"command": "ignored"}, context)

    status_overhead = len(b"Command exited with code 2: ")
    assert len(result.text.encode("utf-8")) <= context.max_output_bytes + status_overhead
    assert result.text.startswith("Command exited with code 2: [truncated] ")
    assert result.text.endswith("traceback final diagnostic 尾")
    assert "\ufffd" not in result.text
    assert result.truncated is True


def test_bash_tool_bounds_output_before_buffering(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path, max_output_bytes=80, max_output_lines=1000)
    python = shlex.quote(sys.executable)
    code = (
        "import sys; "
        "\nfor _ in range(10000): "
        "\n    sys.stdout.write('x' * 1000 + '\\n'); sys.stdout.flush()"
    )
    command = f"{python} -u -c {shlex.quote(code)}"

    result = run_tool(BashTool(), {"command": command, "timeout": 5}, context)

    assert len(str(result.data["stdout"]).encode("utf-8")) <= context.max_output_bytes
    assert result.truncated is True


def test_bash_tool_reports_one_shot_stream_truncation_metadata(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path, max_output_bytes=80, max_output_lines=1000)
    python = shlex.quote(sys.executable)
    code = "import sys; sys.stdout.write('x' * 10000)"
    command = f"{python} -u -c {shlex.quote(code)}"

    result = run_tool(BashTool(), {"command": command, "timeout": 5}, context)

    assert result.data["stdout_truncated"] is True
    assert result.data["stderr_truncated"] is False
    assert result.data["stdout_dropped_bytes"] > 0
    assert result.data["stderr_dropped_bytes"] == 0
    assert result.truncated is True


def test_bash_tool_counts_marker_only_output_as_dropped_bytes(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def fake_run_shell(*args: object, **kwargs: object) -> process_tools_module.ProcessResult:
        return process_tools_module.ProcessResult(exit_code=0, stdout="abcde", stderr="")

    monkeypatch.setattr(shell_tools_module, "_run_shell", fake_run_shell)
    context = ToolContext(cwd=tmp_path, max_output_bytes=5, max_output_lines=0)

    result = run_tool(BashTool(None), {"command": "ignored"}, context)

    assert result.data["stdout"] == "[trun"
    assert result.data["stdout_truncated"] is True
    assert result.data["stdout_dropped_bytes"] == 5
    assert result.truncated is True


def test_bash_tool_counts_source_bytes_when_utf8_clip_decodes_replacement(
    tmp_path: Path,
) -> None:
    context = ToolContext(cwd=tmp_path, max_output_bytes=1, max_output_lines=100)
    python = shlex.quote(sys.executable)
    code = "import sys; sys.stdout.buffer.write(bytes([0xc3, 0xa9]))"
    command = f"{python} -c {shlex.quote(code)}"

    result = run_tool(BashTool(), {"command": command, "timeout": 5}, context)

    assert result.data["stdout"] == "["
    assert result.data["stdout_truncated"] is True
    assert result.data["stdout_dropped_bytes"] == 2
    assert result.truncated is True


def test_bash_tool_counts_retruncated_replacement_source_bytes(
    tmp_path: Path,
) -> None:
    context = ToolContext(cwd=tmp_path, max_output_bytes=15, max_output_lines=100)
    python = shlex.quote(sys.executable)
    code = "import sys; sys.stdout.buffer.write(bytes([0xff] * 15))"
    command = f"{python} -c {shlex.quote(code)}"

    result = run_tool(BashTool(), {"command": command, "timeout": 5}, context)

    assert result.data["stdout"] == "\ufffd\n[truncated]"
    assert result.data["stdout_truncated"] is True
    assert result.data["stdout_dropped_bytes"] == 14
    assert result.truncated is True


def test_bash_tool_counts_managed_source_bytes_when_utf8_clip_decodes_replacement(
    tmp_path: Path,
) -> None:
    async def run() -> tuple[str, int, bool]:
        context = ToolContext(cwd=tmp_path, max_output_bytes=1, max_output_lines=100)
        tool = BashTool()
        python = shlex.quote(sys.executable)
        code = "import sys; sys.stdout.buffer.write(bytes([0xff, 0x61])); sys.stdout.flush()"
        command = f"{python} -c {shlex.quote(code)}"
        try:
            result = await tool.run(
                {
                    "operation": "start",
                    "command": command,
                    "yield_seconds": 0.2,
                    "lifetime_seconds": 5,
                },
                context,
            )
            process_id = str(result.data["process_id"])
            results = [result]
            for _ in range(20):
                if result.data["process_state"] == "completed":
                    break
                result = await tool.run(
                    {
                        "operation": "poll",
                        "process_id": process_id,
                        "wait_seconds": 0.2,
                    },
                    context,
                )
                results.append(result)
            return (
                "".join(str(item.data["stdout"]) for item in results),
                sum(int(item.data["stdout_dropped_bytes"]) for item in results),
                any(item.truncated for item in results),
            )
        finally:
            await tool.aclose()

    stdout, stdout_dropped_bytes, truncated = anyio.run(run)

    assert stdout == "a"
    assert stdout_dropped_bytes == 1
    assert truncated is True


def test_bash_tool_drains_output_past_capture_limit_and_allows_completion(
    tmp_path: Path,
) -> None:
    context = ToolContext(cwd=tmp_path, max_output_bytes=80, max_output_lines=100)
    python = shlex.quote(sys.executable)
    marker = tmp_path / "finished-after-truncation.txt"
    output_bytes = 10_000
    code = (
        "import pathlib, sys; "
        f"sys.stdout.write('x' * {output_bytes}); sys.stdout.flush(); "
        f"pathlib.Path({str(marker)!r}).write_text('ok')"
    )
    command = f"{python} -u -c {shlex.quote(code)}"

    result = run_tool(BashTool(), {"command": command, "timeout": 5}, context)

    assert result.data["exit_code"] == 0
    assert result.data["stdout_truncated"] is True
    assert result.data["stdout_dropped_bytes"] == output_bytes - context.max_output_bytes
    assert result.truncated is True
    assert marker.read_text(encoding="utf-8") == "ok"


def test_bash_tool_does_not_kill_process_at_exact_output_limit(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path, max_output_bytes=100, max_output_lines=1)
    python = shlex.quote(sys.executable)
    marker = tmp_path / "finished.txt"
    code = (
        "import pathlib, time; "
        "print('done'); time.sleep(0.2); "
        f"pathlib.Path({str(marker)!r}).write_text('ok')"
    )
    command = f"{python} -c {shlex.quote(code)}"

    result = run_tool(BashTool(), {"command": command, "timeout": 5}, context)

    assert result.data["stdout"] == "done\n"
    assert result.truncated is False
    assert marker.read_text(encoding="utf-8") == "ok"


def test_bash_tool_reports_timeout_and_kills_child_processes(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)
    python = shlex.quote(sys.executable)
    marker = tmp_path / "child-survived.txt"
    child_code = (
        f"import pathlib, time; time.sleep(1.5); pathlib.Path({str(marker)!r}).write_text('alive')"
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(5)"
    )
    command = f"{python} -c {shlex.quote(parent_code)}"

    with pytest.raises(ToolError, match="timed out"):
        run_tool(BashTool(), {"command": command, "timeout": 1}, context)
    time.sleep(1.0)

    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group assertion")
def test_bash_completion_kills_background_child_with_redirected_output(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)
    child_pid_path = tmp_path / "background.pid"
    command = f"sleep 30 >/dev/null 2>&1 & echo $! > {shlex.quote(str(child_pid_path))}"

    result = run_tool(BashTool(), {"command": command, "timeout": 5}, context)
    child_pid = int(child_pid_path.read_text())

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("background child remained alive after bash completion")

    assert result.data["exit_code"] == 0


def test_bash_tool_reports_process_tree_cleanup_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    original_terminate = process_manager_module._terminate_process_tree  # type: ignore[attr-defined]
    cleanup_attempts = 0

    async def fail_terminate(process: asyncio.subprocess.Process) -> bool:
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        await original_terminate(process)
        return cleanup_attempts > 1

    monkeypatch.setattr(process_manager_module, "_terminate_process_tree", fail_terminate)

    context = ToolContext(cwd=tmp_path)
    source = "print('done')"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"
    tool = BashTool()

    with pytest.raises(ToolError, match="Failed to terminate process tree"):
        run_tool(tool, {"command": command, "timeout": 5}, context)
    supervisor = tool._process_supervisor  # noqa: SLF001
    assert supervisor is not None
    assert len(supervisor._one_shot) == 1  # noqa: SLF001
    anyio.run(tool.aclose)
    assert len(supervisor._one_shot) == 0  # noqa: SLF001
    assert cleanup_attempts == 2


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group assertion")
def test_bash_timeout_kills_background_child_after_shell_exits(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)
    marker = tmp_path / "background-child-survived.txt"
    command = f"(sleep 1.5; echo alive > {shlex.quote(str(marker))}) &"

    with pytest.raises(ToolError, match="timed out"):
        run_tool(BashTool(), {"command": command, "timeout": 1}, context)
    time.sleep(1.0)

    assert not marker.exists()


def test_bash_tool_cancellation_kills_child_processes(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX process-group cancellation regression")
    context = ToolContext(cwd=tmp_path)
    python = shlex.quote(sys.executable)
    ready = tmp_path / "parent-ready.txt"
    marker = tmp_path / "child-survived.txt"
    child_code = (
        f"import pathlib, time; time.sleep(1.0); pathlib.Path({str(marker)!r}).write_text('alive')"
    )
    parent_code = (
        "import pathlib, subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"pathlib.Path({str(ready)!r}).write_text('ready'); "
        "time.sleep(5)"
    )
    command = f"{python} -c {shlex.quote(parent_code)}"

    async def run_and_cancel() -> None:
        async def run_bash() -> None:
            try:
                await BashTool().run({"command": command, "timeout": 10}, context)
            except anyio.get_cancelled_exc_class():
                pass

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run_bash)
            with anyio.fail_after(2):
                while not ready.exists():
                    await anyio.sleep(0.05)
            task_group.cancel_scope.cancel()

    anyio.run(run_and_cancel)
    time.sleep(1.3)

    assert not marker.exists()


def test_direct_bash_cancellation_surfaces_cleanup_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pass

    process = DummyProcess()

    async def create_process(_command: str, *, cwd: Path) -> DummyProcess:
        assert cwd == tmp_path
        return process

    async def fail_cleanup(captured_process: object) -> bool:
        assert captured_process is process
        return False

    monkeypatch.setattr(process_tools_module, "_create_shell_process", create_process)
    monkeypatch.setattr(process_tools_module, "_kill_process_tree_and_wait", fail_cleanup)

    async def run_and_cancel() -> None:
        collect_started = asyncio.Event()

        async def collect_output(*_args: object, **_kwargs: object) -> tuple[bytes, bytes]:
            collect_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        monkeypatch.setattr(process_tools_module, "_collect_limited_output", collect_output)
        task = asyncio.create_task(
            process_tools_module._run_shell(  # noqa: SLF001
                "ignored",
                cwd=tmp_path,
                timeout=10,
                max_output_bytes=100,
                max_output_lines=100,
            )
        )
        await collect_started.wait()
        task.cancel()
        with pytest.raises(ToolError, match="Failed to terminate process tree"):
            await task

    anyio.run(run_and_cancel)


def test_direct_bash_cleanup_finishes_before_raw_task_cancellation(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    context = ToolContext(cwd=tmp_path)
    python = shlex.quote(sys.executable)
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleanup_finished = False
    original_terminate = process_tools_module._terminate_process_tree  # type: ignore[attr-defined]

    async def delayed_terminate(
        process: asyncio.subprocess.Process,
        *,
        force: bool = False,
    ) -> bool:
        nonlocal cleanup_finished
        cleanup_started.set()
        await allow_cleanup.wait()
        cleanup_succeeded = await original_terminate(process, force=force)
        cleanup_finished = True
        return cleanup_succeeded

    monkeypatch.setattr(process_tools_module, "_terminate_process_tree", delayed_terminate)

    async def run_and_cancel() -> None:
        task = asyncio.create_task(
            BashTool(None).run(
                {"command": f"{python} -c {shlex.quote("print('done')")}", "timeout": 10},
                context,
            )
        )
        await cleanup_started.wait()
        task.cancel()
        await anyio.sleep(0)
        assert task.done() is False

        allow_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    anyio.run(run_and_cancel)

    assert cleanup_finished is True


def test_direct_bash_cleans_process_after_capture_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyStream:
        def at_eof(self) -> bool:
            return True

    class DummyProcess:
        returncode: int | None = None
        stdout = DummyStream()
        stderr = DummyStream()
        wait_count = 0

        async def wait(self) -> None:
            self.wait_count += 1
            self.returncode = -15

    process = DummyProcess()
    cleanup_calls = 0

    async def create_process(_command: str, *, cwd: Path) -> DummyProcess:
        assert cwd == tmp_path
        return process

    async def fail_collect(*_args: object, **_kwargs: object) -> tuple[bytes, bytes]:
        raise ToolError("capture failed")

    async def terminate_process(
        captured_process: object,
        *,
        force: bool = False,
    ) -> bool:
        nonlocal cleanup_calls
        assert captured_process is process
        assert force is False
        cleanup_calls += 1
        return True

    monkeypatch.setattr(process_tools_module, "_create_shell_process", create_process)
    monkeypatch.setattr(process_tools_module, "_collect_limited_output", fail_collect)
    monkeypatch.setattr(process_tools_module, "_terminate_process_tree", terminate_process)

    async def run() -> None:
        await process_tools_module._run_shell(  # noqa: SLF001
            "ignored",
            cwd=tmp_path,
            timeout=5,
            max_output_bytes=100,
            max_output_lines=100,
        )

    with pytest.raises(ToolError, match="capture failed"):
        anyio.run(run)
    assert cleanup_calls == 1
    assert process.wait_count == 1
