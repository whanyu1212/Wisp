from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import ToolApprovalRequested, ToolApprovalReviewed, ToolExecutionEnded
from wisp.providers.events import ToolCall
from wisp.runtime.registry import ToolRegistry
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.command_review import command_review_objection
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolArgumentError
from wisp.tools.shell.tool import BashTool
from wisp.tools.shell.working_directory import command_context


def test_relative_cwd_preserves_workspace_and_argument_resolution(tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    context = ToolContext(cwd=tmp_path)
    scoped = command_context({"cwd": "site"}, context)
    assert scoped.cwd == site and scoped.boundary() == tmp_path
    assert context.cwd == tmp_path
    assert command_review_objection({"cwd": "site", "command": "npm test"}, context) is None
    assert command_review_objection({"cwd": "site", "command": "pytest ../../outside.py"}, context)


@pytest.mark.parametrize(
    "value",
    ["../outside", "missing", ".git", ".env", "private", "escape", "secret-link", "", None, 42],
)
def test_cwd_rejects_invalid_or_protected_locations(tmp_path: Path, value: object) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    for name in (".git", ".env", "private"):
        (workspace / name).mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    (workspace / "secret-link").symlink_to(workspace / "private", target_is_directory=True)
    context = ToolContext(cwd=workspace, protected_paths=(".env", "private"))
    with pytest.raises(ToolArgumentError):
        command_context({"cwd": value}, context)
    assert command_review_objection({"cwd": value, "command": "npm test"}, context)


def test_cwd_does_not_accept_an_outside_alias_back_into_workspace(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ToolArgumentError):
        command_context({"cwd": str(alias)}, ToolContext(cwd=root))


@pytest.mark.parametrize("operation", ["poll", "cancel"])
def test_cwd_is_not_a_process_control_argument(tmp_path: Path, operation: str) -> None:
    with pytest.raises(ToolArgumentError):
        command_context({"cwd": ".", "operation": operation}, ToolContext(cwd=tmp_path))


@pytest.mark.process
@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt")
def test_fast_path_runs_in_explicit_directory(tmp_path: Path) -> None:
    site, scratch = tmp_path / "site", tmp_path / "scratch"
    site.mkdir()
    scratch.mkdir()
    (site / "test_cwd.py").write_text(
        "from pathlib import Path\ndef test_cwd():\n    assert Path.cwd().name == 'site'\n"
    )
    context = ToolContext(cwd=tmp_path, scratch_root=scratch)

    async def run() -> None:
        tool = BashTool()
        registry = ToolRegistry()
        registry.register(tool)
        runner = ConfiguredToolExecutor(
            registry=registry,
            context=context,
            policy=ToolPolicy.allow_all_tools(),
            approval_policy=ToolApprovalPolicy.auto_commands(),
        )
        try:
            events = [
                event
                async for event in runner.execute(
                    ToolCall(
                        call_id="cwd",
                        name="bash",
                        arguments={"cwd": "site", "command": "python -m pytest -q test_cwd.py"},
                    )
                )
            ]
            assert not any(
                isinstance(event, ToolApprovalRequested | ToolApprovalReviewed) for event in events
            )
            assert isinstance(events[-1], ToolExecutionEnded) and events[-1].exit_code == 0, events
        finally:
            await tool.aclose()

    asyncio.run(run())


@pytest.mark.process
@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt")
def test_prepared_directory_cannot_be_rebound(tmp_path: Path) -> None:
    from wisp.agent.tool_contracts import PreparedToolExecution

    site, other, scratch = (tmp_path / name for name in ("site", "other", "scratch"))
    for directory in (site, other, scratch):
        directory.mkdir()
    context = ToolContext(cwd=tmp_path, scratch_root=scratch)

    async def run() -> None:
        tool = BashTool()
        registry = ToolRegistry()
        registry.register(tool)
        runner = ConfiguredToolExecutor(
            registry=registry,
            context=context,
            policy=ToolPolicy.allow_all_tools(),
            approval_policy=ToolApprovalPolicy.auto_commands(),
        )
        try:
            call = ToolCall(
                call_id="cwd",
                name="bash",
                arguments={"cwd": "site", "command": "python -m pytest -q"},
            )
            prepared_events = [event async for event in runner.prepare(call)]
            prepared = prepared_events[-1]
            assert isinstance(prepared, PreparedToolExecution)
            site.rename(tmp_path / "original")
            site.symlink_to(other, target_is_directory=True)
            ended = await prepared.run()
            assert ended.is_error and "no longer matches" in ended.output
        finally:
            await tool.aclose()

    asyncio.run(run())
