"""Codemode mutations retain policy, lifecycle, and process ownership."""

from __future__ import annotations

import itertools
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import anyio
import pytest

from tests.coding.test_codemode import _execute, _Tool
from tests.tools.shell.supervisor_support import python_command, wait_for_pid_file
from wisp.coding.tool_execution import BackgroundRequests, ConfiguredToolExecutor
from wisp.coding.tool_progress import LiveEventSink
from wisp.events import (
    ToolExecutionEnded,
    ToolExecutionProgress,
    ToolNestedCallEnded,
    ToolNestedCallStarted,
    WispEvent,
)
from wisp.providers.events import ToolCall
from wisp.runtime.registry import ToolRegistry
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.base import Tool, ToolArguments, ToolExecutionMetadata
from wisp.tools.codemode.calls import NestedCallResult
from wisp.tools.codemode.sandbox import ScriptLimits
from wisp.tools.codemode.tool import CodemodeTool
from wisp.tools.context import ToolContext
from wisp.tools.files.operations import EditTool, ReadTool, WriteTool
from wisp.tools.policy import ToolPolicy
from wisp.tools.progress import OutputProgress
from wisp.tools.result import ToolResult
from wisp.tools.seen_files import SeenFiles
from wisp.tools.shell.supervisor import ProcessSupervisor
from wisp.tools.shell.tool import BashTool


def _executor(
    context: ToolContext,
    tools: Sequence[Tool],
    *,
    limits: ScriptLimits | None = None,
    timeline: list[WispEvent] | None = None,
    sink: LiveEventSink | None = None,
    policy: ToolPolicy | None = None,
    background_requests: BackgroundRequests | None = None,
) -> ConfiguredToolExecutor:
    registry = ToolRegistry()
    ids = itertools.count()

    async def call(name: str, arguments: Mapping[str, object]) -> NestedCallResult:
        return await executor.run_codemode(
            ToolCall(call_id=f"child-{next(ids)}", name=name, arguments=dict(arguments))
        )

    async def live(event: WispEvent) -> None:
        if timeline is not None:
            timeline.append(event)
        if sink is not None:
            await sink(event)

    for tool in tools:
        # Mutations must serialize even if an extension declares them parallel-safe.
        registry.register(tool, execution=ToolExecutionMetadata(parallel_safe=True))
    registry.register(CodemodeTool(tools, call, limits=limits))
    executor = ConfiguredToolExecutor(
        registry=registry,
        context=context,
        policy=policy or ToolPolicy.allow_all_tools(),
        approval_policy=ToolApprovalPolicy.approve_all(),
        progress_sink=live,
        background_requests=background_requests,
    )
    return executor


def test_read_edit_write_workflow(tmp_path: Path) -> None:
    (tmp_path / "source.txt").write_text("before")
    executor = _executor(
        ToolContext(cwd=tmp_path, seen_files=SeenFiles()), [ReadTool(), EditTool(), WriteTool()]
    )
    code = (
        'await read(path="source.txt")\n'
        'await edit(path="source.txt", edits=[{"oldText": "before", "newText": "after"}])\n'
        'await write(path="copy.txt", content=await read(path="source.txt"))'
    )
    ended = anyio.run(_execute, executor, "script", code)
    assert not ended.is_error
    assert (tmp_path / "source.txt").read_text() == "after"
    assert "after" in (tmp_path / "copy.txt").read_text()
    assert [call.name for call in ended.nested_calls] == ["read", "edit", "read", "write"]


@pytest.mark.parametrize("path", ["secret.txt", "../outside.txt"])
def test_write_boundary_is_preserved(tmp_path: Path, path: str) -> None:
    executor = _executor(ToolContext(cwd=tmp_path, protected_paths=("secret.txt",)), [WriteTool()])
    ended = anyio.run(
        _execute, executor, "script", f"await write(path={path!r}, content='forbidden')"
    )
    assert ended.is_error and ended.nested_calls[0].is_error
    assert not (tmp_path / path).exists()
    assert "protected" in ended.output if path == "secret.txt" else "outside" in ended.output


def test_policy_blocks_mutation(tmp_path: Path) -> None:
    executor = _executor(
        ToolContext(cwd=tmp_path),
        [WriteTool()],
        policy=ToolPolicy.allow_tool_names({"codemode"}),
    )
    ended = anyio.run(
        _execute, executor, "script", 'await write(path="blocked.txt", content="forbidden")'
    )
    assert ended.is_error and ended.nested_calls[0].failure_code == "policy_denied"
    assert not (tmp_path / "blocked.txt").exists()


def test_mutations_are_serialized(tmp_path: Path) -> None:
    tool = _Tool("change", safety="mutating", delay=0.02)
    executor = _executor(ToolContext(cwd=tmp_path), [tool])
    code = 'import asyncio\nawait asyncio.gather(*[change(table=t) for t in ["a", "b", "c"]])'
    ended = anyio.run(_execute, executor, "script", code)
    assert not ended.is_error
    assert tool.most_running == 1 and len(tool.calls) == 3


@pytest.mark.parametrize("name", ["codemode", "spawn_agent"])
def test_recursive_hosts_are_blocked(tmp_path: Path, name: str) -> None:
    executor = _executor(ToolContext(cwd=tmp_path), [])

    async def run() -> None:
        result = await executor.run_codemode(ToolCall(call_id="recursive", name=name, arguments={}))
        assert result.is_error and "cannot" in result.text

    anyio.run(run)


def test_parent_joins_mutation_cleanup(tmp_path: Path) -> None:
    async def run() -> ToolExecutionEnded:
        cleanup_started = anyio.Event()
        release_cleanup = anyio.Event()
        cleaned = False

        class Mutation(_Tool):
            async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
                nonlocal cleaned
                self.calls.append(dict(arguments))
                try:
                    await anyio.sleep_forever()
                finally:
                    with anyio.CancelScope(shield=True):
                        cleanup_started.set()
                        await release_cleanup.wait()
                        cleaned = True

        tool = Mutation("change", safety="mutating")
        executor = _executor(
            ToolContext(cwd=tmp_path), [tool], limits=ScriptLimits(wall_seconds=0.2)
        )
        completed = anyio.Event()

        async def release() -> None:
            await cleanup_started.wait()
            await anyio.wait_all_tasks_blocked()
            assert not completed.is_set()
            release_cleanup.set()

        async with anyio.create_task_group() as group:
            group.start_soon(release)
            ended = await _execute(
                executor,
                "script",
                "import asyncio\n"
                'await asyncio.gather(change(table="first"), change(table="queued"))',
            )
            completed.set()
        assert cleaned and tool.calls == [{"table": "first"}]
        return ended

    ended = anyio.run(run)
    assert ended.is_error
    assert [call.error for call in ended.nested_calls] == [
        "The script ended before this call finished"
    ] * 2


@pytest.mark.process
def test_bash_progress_uses_parent(tmp_path: Path) -> None:
    timeline: list[WispEvent] = []

    async def run() -> None:
        supervisor = ProcessSupervisor(max_processes=1)
        try:
            requests = BackgroundRequests()
            executor = _executor(
                ToolContext(cwd=tmp_path),
                [BashTool(supervisor)],
                timeline=timeline,
                background_requests=requests,
            )
            command = python_command("import time; print('working', flush=True); time.sleep(1)")
            ended = await _execute(executor, "script", f"await bash(command={command!r})")
            timeline.append(ended)
            assert not ended.is_error
            assert not requests.request("child-0")
        finally:
            await supervisor.aclose()

    anyio.run(run)
    progress = [event for event in timeline if isinstance(event, ToolExecutionProgress)]
    assert progress and "working" in progress[-1].stdout_tail
    assert all(event.call_id == "script" and event.name == "codemode" for event in progress)
    assert [event.sequence for event in progress] == list(range(1, len(progress) + 1))
    assert isinstance(timeline[0], ToolNestedCallStarted)
    assert isinstance(timeline[-2], ToolNestedCallEnded)
    assert isinstance(timeline[-1], ToolExecutionEnded)


@pytest.mark.parametrize("overlap", [False, True], ids=["sequential", "overlapping"])
@pytest.mark.parametrize("second_stdout", [False, True], ids=["stderr-only", "mixed-streams"])
def test_child_progress_accumulates_bytes(
    tmp_path: Path, overlap: bool, second_stdout: bool
) -> None:
    async def run() -> list[ToolExecutionProgress]:
        first_seen = anyio.Event()
        second_seen = anyio.Event()
        third_seen = anyio.Event()
        first_sent = anyio.Event()
        second_sent = anyio.Event()
        snapshots: list[ToolExecutionProgress] = []

        async def live(event: WispEvent) -> None:
            if isinstance(event, ToolExecutionProgress):
                previous_stdout = snapshots[-1].stdout_bytes if snapshots else 0
                snapshots.append(event)
                moving_tail = (
                    event.stdout_tail if event.stdout_bytes > previous_stdout else event.stderr_tail
                )
                match moving_tail:
                    case "long-first-stderr":
                        first_seen.set()
                    case "b":
                        second_seen.set()
                    case "long-first-stderr++":
                        third_seen.set()

        class ReportingTool(_Tool):
            async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
                progress = context.progress
                assert progress is not None

                async def report(text: str, observed: anyio.Event, *, stdout: bool = False) -> None:
                    progress.report(
                        OutputProgress(
                            text if stdout else "",
                            "" if stdout else text,
                            stdout_bytes=len(text) if stdout else 0,
                            stderr_bytes=0 if stdout else len(text),
                        )
                    )
                    await observed.wait()

                if arguments["table"] == "first":
                    await report("long-first-stderr", first_seen)
                    first_sent.set()
                    if overlap:
                        await second_sent.wait()
                        await report("long-first-stderr++", third_seen)
                else:
                    await first_sent.wait()
                    await report("b", second_seen, stdout=second_stdout)
                    second_sent.set()
                return ToolResult(text="done")

        executor = _executor(ToolContext(cwd=tmp_path), [ReportingTool("report")], sink=live)
        code = (
            'import asyncio\nawait asyncio.gather(report(table="first"), report(table="second"))'
            if overlap
            else 'await report(table="first")\nawait report(table="second")'
        )
        with anyio.fail_after(5):
            ended = await _execute(executor, "script", code)
        assert not ended.is_error
        return snapshots

    snapshots = anyio.run(run)
    expected_stderr = [17, 17, 19] if second_stdout else [17, 18, 20]
    expected_stdout = [0, 1, 1] if second_stdout else [0, 0, 0]
    count = 3 if overlap else 2
    assert [event.stderr_bytes for event in snapshots] == expected_stderr[:count]
    assert [event.stdout_bytes for event in snapshots] == expected_stdout[:count]
    assert snapshots[1].stderr_tail == ("long-first-stderr" if second_stdout else "b")
    assert [event.sequence for event in snapshots] == list(range(1, len(snapshots) + 1))


@pytest.mark.process
def test_bash_cancel_releases_process(tmp_path: Path) -> None:
    async def run() -> None:
        supervisor = ProcessSupervisor(max_processes=1)
        pid_path = tmp_path / "pid"
        command = python_command(
            f"import os,time; from pathlib import Path; "
            f"Path({str(pid_path)!r}).write_text(str(os.getpid())); time.sleep(60)"
        )
        try:
            executor = _executor(ToolContext(cwd=tmp_path), [BashTool(supervisor)])
            with anyio.CancelScope() as scope:

                async def cancel() -> None:
                    await wait_for_pid_file(pid_path)
                    scope.cancel()

                async with anyio.create_task_group() as group:
                    group.start_soon(cancel)
                    await _execute(executor, "script", f"await bash(command={command!r})")
            pid = int(pid_path.read_text())
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
            # Capacity one proves the cancelled foreground call relinquished ownership.
            result = await supervisor.run_to_completion(
                "echo next", cwd=tmp_path, timeout=5, max_output_bytes=100
            )
            assert result.exit_code == 0 and "next" in result.stdout
        finally:
            await supervisor.aclose()

    anyio.run(run)


@pytest.mark.process
@pytest.mark.parametrize("stop", ["cancel", "shutdown"])
def test_managed_process_survives_script(tmp_path: Path, stop: str) -> None:
    async def run() -> None:
        supervisor = ProcessSupervisor(max_processes=1)
        try:
            executor = _executor(ToolContext(cwd=tmp_path), [BashTool(supervisor)])
            pid_path = tmp_path / "managed-pid"
            command = python_command(
                f"import os,time; from pathlib import Path; "
                f"Path({str(pid_path)!r}).write_text(str(os.getpid())); time.sleep(60)"
            )
            code = (
                f"await bash(operation='start', command={command!r}, yield_seconds=0)\n"
                "raise ValueError('script failed after start')"
            )
            ended = await _execute(executor, "script", code)
            assert ended.is_error and not ended.nested_calls[0].is_error
            await wait_for_pid_file(pid_path)
            (process,) = supervisor.processes()
            assert process.state == "running"
            if stop == "cancel":
                result = await executor.run_codemode(
                    ToolCall(
                        call_id="cancel",
                        name="bash",
                        arguments={"operation": "cancel", "process_id": process.process_id},
                    )
                )
                assert not result.is_error
                assert (await supervisor.poll(process.process_id)).state == "cancelled"
            else:
                await supervisor.aclose()
                assert not supervisor.running_processes()
        finally:
            await supervisor.aclose()

    anyio.run(run)
