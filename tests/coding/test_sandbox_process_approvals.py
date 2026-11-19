"""Automatic process control is authority over a specific job, not all Bash calls."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from wisp.agent.tool_contracts import PreparedToolExecution
from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import (
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolApprovalReviewed,
    ToolExecutionEnded,
)
from wisp.providers.events import ToolCall
from wisp.runtime.registry import ToolRegistry
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.context import ToolContext, WriteConstraint
from wisp.tools.policy import ToolPolicy
from wisp.tools.shell.tool import BashTool

pytestmark = [pytest.mark.process, pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt")]


@pytest.fixture
def context(tmp_path: Path) -> ToolContext:
    root, scratch = tmp_path / "workspace", tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()
    (root / "test_wait.py").write_text("import time\ndef test_wait():\n    time.sleep(1)\n")
    return ToolContext(cwd=root, scratch_root=scratch)


def executor(
    context: ToolContext, tool: BashTool, policy: ToolApprovalPolicy
) -> ConfiguredToolExecutor:
    registry = ToolRegistry()
    registry.register(tool)
    return ConfiguredToolExecutor(
        registry=registry,
        context=context,
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=policy,
    )


async def start(runner: ConfiguredToolExecutor, *, foreground: bool = False) -> str:
    call = ToolCall(
        call_id="start",
        name="bash",
        arguments={
            "operation": "run" if foreground else "start",
            "command": "python -m pytest -q test_wait.py",
            "yield_seconds": 0,
        },
    )
    events = [event async for event in runner.execute(call)]
    assert not any(
        isinstance(event, ToolApprovalRequested | ToolApprovalReviewed) for event in events
    )
    ended = events[-1]
    assert isinstance(ended, ToolExecutionEnded) and ended.process_id is not None, events
    return ended.process_id


@pytest.mark.parametrize("foreground", [False, True])
def test_owned_job_poll_and_cancel_need_neither_human_nor_model(
    context: ToolContext, foreground: bool
) -> None:
    async def run() -> None:
        tool = BashTool()
        policy = ToolApprovalPolicy.auto_commands()
        try:
            background = asyncio.Event()
            background.set()
            runner = executor(replace(context, background_request=background), tool, policy)
            process_id = await start(runner, foreground=foreground)
            # Another executor in the same conversation/run context retains access.
            runner = executor(replace(context), tool, policy)
            for operation in ("poll", "cancel"):
                events = [
                    event
                    async for event in runner.execute(
                        ToolCall(
                            call_id=operation,
                            name="bash",
                            arguments={"operation": operation, "process_id": process_id},
                        )
                    )
                ]
                assert not any(
                    isinstance(event, ToolApprovalRequested | ToolApprovalReviewed)
                    for event in events
                )
                assert any(
                    isinstance(event, ToolApprovalResolved)
                    and event.rule_id == "sandbox_process_v1"
                    for event in events
                )
                assert isinstance(events[-1], ToolExecutionEnded) and not events[-1].is_error
            assert events[-1].process_state == "cancelled"
        finally:
            await tool.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "restriction",
    [
        "owner",
        "workspace",
        "generation",
        "policy",
        "mode",
        "write",
        "atomic",
        "outside",
        "unknown-id",
        "hidden",
        "extra-command",
    ],
)
def test_job_grant_cannot_cross_authority_boundaries(
    context: ToolContext, restriction: str
) -> None:
    async def run() -> None:
        tool = BashTool()
        policy = ToolApprovalPolicy.auto_commands()
        try:
            runner = executor(context, tool, policy)
            process_id = await start(runner)
            other = context
            if restriction == "owner":
                other = replace(context, process_owner=object())
            elif restriction == "workspace":
                root = context.cwd.parent / "other"
                root.mkdir()
                other = replace(context, cwd=root)
            elif restriction == "generation":
                policy.generation += 1
            elif restriction == "policy":
                policy = ToolApprovalPolicy.auto_commands()
            elif restriction == "mode":
                policy.mode = "auto"
            elif restriction == "write":
                other = replace(context, write_constraint=WriteConstraint())
            elif restriction == "atomic":
                other = replace(context, atomic_writes_only=True)
            elif restriction == "outside":
                other = replace(context, allow_outside_cwd=True)
            elif restriction == "unknown-id":
                process_id = "not-an-owned-process"
            elif restriction == "hidden":
                assert tool._process_supervisor is not None
                tool._process_supervisor.unlist_all()
            call = ToolCall(
                call_id="poll",
                name="bash",
                arguments={"operation": "poll", "process_id": process_id},
            )
            if restriction == "extra-command":
                call.arguments["command"] = "python -m pytest"
            events = [event async for event in executor(other, tool, policy).execute(call)]
            assert any(isinstance(event, ToolApprovalRequested) for event in events)
            assert (
                isinstance(events[-1], ToolExecutionEnded)
                and events[-1].failure_code == "approval_denied"
            )
        finally:
            await tool.aclose()

    asyncio.run(run())


def test_unrestricted_jobs_do_not_acquire_automatic_control(context: ToolContext) -> None:
    async def run() -> None:
        tool = BashTool()
        try:
            result = await tool.run(
                {"operation": "start", "command": "sleep 1", "yield_seconds": 0}, context
            )
            process_id = result.data["process_id"]
            assert isinstance(process_id, str)
            runner = executor(context, tool, ToolApprovalPolicy.auto_commands())
            events = [
                event
                async for event in runner.execute(
                    ToolCall(
                        call_id="poll",
                        name="bash",
                        arguments={"operation": "poll", "process_id": process_id},
                    )
                )
            ]
            assert any(isinstance(event, ToolApprovalRequested) for event in events)
        finally:
            await tool.aclose()

    asyncio.run(run())


def test_deferred_control_is_revoked_before_execution(context: ToolContext) -> None:
    async def run() -> None:
        tool = BashTool()
        policy = ToolApprovalPolicy.auto_commands()
        try:
            runner = executor(context, tool, policy)
            process_id = await start(runner)
            call = ToolCall(
                call_id="cancel",
                name="bash",
                arguments={"operation": "cancel", "process_id": process_id},
            )
            events = [event async for event in runner.prepare(call)]
            prepared = events[-1]
            assert isinstance(prepared, PreparedToolExecution)
            policy.generation += 1
            ended = await prepared.run()
            assert ended.failure_code == "approval_denied"
        finally:
            await tool.aclose()

    asyncio.run(run())
