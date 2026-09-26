from __future__ import annotations

import json
import os
from pathlib import Path

import anyio
import pytest

import wisp.trust.permissions as permissions
from wisp.config.runtime import WispConfig
from wisp.rpc.host import RpcToolApprovalPolicy
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.builtin import BashTool, WriteTool
from wisp.tools.context import ToolContext
from wisp.tools.files.paths import resolve_tool_path
from wisp.tools.result import ToolError
from wisp.trust.permissions import load_permission_mode, permissions_directory, save_permission_mode


def test_rpc_permission_command_round_trips_across_new_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.support.cli import CliRunner, app

    monkeypatch.chdir(tmp_path)

    def run(commands: list[dict[str, object]]) -> list[dict[str, object]]:
        result = CliRunner().invoke(
            app,
            ["--mode", "rpc", "--provider", "fake", "--session-dir", str(tmp_path / "sessions")],
            input="".join(json.dumps(command) + "\n" for command in commands),
        )
        assert result.exit_code == 0, result.output
        records = [json.loads(line) for line in result.stdout.splitlines()]
        assert not any(record["type"] == "error" for record in records), records
        assert all(record.get("ok", True) for record in records), records
        return [record["permissions"] for record in records if record["type"] == "rpc.permissions"]

    enabled = run([{"type": "set_permissions", "id": "enable", "mode": "yolo"}])
    assert enabled[0]["mode"] == enabled[0]["saved_mode"] == "yolo"
    restarted = run(
        [
            {"type": "get_permissions", "id": "read"},
            {"type": "set_permissions", "id": "revoke", "mode": "ask"},
        ]
    )
    assert restarted[0]["mode"] == "yolo"
    assert restarted[1]["mode"] == restarted[1]["saved_mode"] == "ask"
    assert run([{"type": "get_permissions", "id": "verify"}])[0]["mode"] == "ask"


def test_modes_are_isolated_by_canonical_project_and_survive_restarts(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(project, target_is_directory=True)
    assert load_permission_mode(project) is None
    save_permission_mode(alias, "yolo")
    assert load_permission_mode(project) == "yolo"
    assert load_permission_mode(tmp_path / "other") is None
    save_permission_mode(tmp_path / "other", "yolo")
    save_permission_mode(project, "ask")
    assert load_permission_mode(alias) == "ask"
    assert load_permission_mode(tmp_path / "other") == "yolo"
    if os.name == "posix":
        assert permissions_directory().stat().st_mode & 0o777 == 0o700
        assert all(
            path.stat().st_mode & 0o777 == 0o600 for path in permissions_directory().iterdir()
        )


@pytest.mark.parametrize("content", ["{", "[]", '["yolo"]', "[" * 1500 + "0" + "]" * 1500])
def test_corrupt_preferences_fail_closed(tmp_path: Path, content: str) -> None:
    save_permission_mode(tmp_path, "yolo")
    path = next(permissions_directory().iterdir())
    path.write_text(content)
    assert load_permission_mode(tmp_path) is None


def test_invalid_project_record_and_repo_settings_cannot_enable_yolo(tmp_path: Path) -> None:
    project_settings = tmp_path / ".wisp" / "settings.json"
    project_settings.parent.mkdir()
    project_settings.write_text(json.dumps({"permission_mode": "yolo"}))
    assert load_permission_mode(tmp_path) is None
    save_permission_mode(tmp_path, "ask")
    path = next(permissions_directory().iterdir())
    path.write_text(json.dumps({"project": str(tmp_path / "other"), "mode": "yolo"}))
    assert load_permission_mode(tmp_path) is None


def test_symlinked_preference_is_not_loaded(tmp_path: Path) -> None:
    save_permission_mode(tmp_path, "yolo")
    path = next(permissions_directory().iterdir())
    target = tmp_path / "injected.json"
    path.rename(target)
    path.symlink_to(target)
    assert load_permission_mode(tmp_path) is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes and named pipes")
def test_insecure_and_nonregular_preferences_fail_closed(tmp_path: Path) -> None:
    save_permission_mode(tmp_path, "yolo")
    path = next(permissions_directory().iterdir())
    path.chmod(0o644)
    assert load_permission_mode(tmp_path) is None
    path.unlink()
    os.mkfifo(path, 0o600)
    assert load_permission_mode(tmp_path) is None
    path.unlink()
    permissions_directory().chmod(0o777)
    with pytest.raises(PermissionError):
        save_permission_mode(tmp_path, "yolo")


def test_failed_replace_preserves_saved_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_permission_mode(tmp_path, "ask")

    def fail(*_args: object) -> None:
        raise OSError("disk failure")

    monkeypatch.setattr(permissions.os, "replace", fail)
    with pytest.raises(OSError):
        save_permission_mode(tmp_path, "yolo")
    assert load_permission_mode(tmp_path) == "ask"
    assert len(list(permissions_directory().iterdir())) == 1


def test_preference_files_are_protected_even_with_empty_configured_paths(tmp_path: Path) -> None:
    save_permission_mode(tmp_path, "ask")
    preference = next(permissions_directory().iterdir())
    config = WispConfig(protected_paths=())
    context = ToolContext.from_config(config, cwd=Path.home())
    with pytest.raises(ToolError, match="protected"):
        resolve_tool_path(str(preference), context)
    alias = Path.home() / "permission-alias.json"
    alias.symlink_to(preference)
    with pytest.raises(ToolError, match="protected"):
        resolve_tool_path(str(alias), context)


def test_temporary_grants_expire_but_yolo_and_ask_are_saved(tmp_path: Path) -> None:
    def fresh() -> RpcToolApprovalPolicy:
        return RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval(), project_path=tmp_path)

    policy = fresh()
    tool = WriteTool()
    policy.prepare_approval(tool, call_id="once", arguments={})
    assert policy.resolve_approval(call_id="once", approved=True)
    assert not policy.approves(tool)
    policy.prepare_approval(tool, call_id="session", arguments={})
    assert policy.resolve_approval(call_id="session", approved=True, scope="tool_session")
    assert policy.approves(tool)
    assert not policy.approves(BashTool())
    assert not fresh().approves(tool)
    policy.prepare_approval(BashTool(), call_id="yolo", arguments={})
    assert policy.resolve_approval(call_id="yolo", approved=True, scope="all_project")
    assert fresh().approves(tool)
    assert fresh().approves(BashTool())
    policy.set_permissions("ask")
    assert not policy.approves(tool)
    assert not fresh().approves(tool)


def test_stale_yolo_response_cannot_persist_or_widen_permissions(tmp_path: Path) -> None:
    policy = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval(), project_path=tmp_path)
    policy.prepare_approval(BashTool(), call_id="call", arguments={})
    assert policy.resolve_approval(call_id="call", approved=False)
    assert not policy.resolve_approval(call_id="call", approved=True, scope="all_project")
    assert load_permission_mode(tmp_path) is None
    assert not policy.approves(BashTool())


def test_prepared_denial_survives_another_calls_yolo_approval(tmp_path: Path) -> None:
    async def scenario() -> None:
        policy = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval(), project_path=tmp_path)
        tool = BashTool()
        for call in ("denied", "waiting", "approved"):
            policy.prepare_approval(tool, call_id=call, arguments={})
        assert policy.resolve_approval(call_id="denied", approved=False, reason="no")
        assert policy.resolve_approval(call_id="approved", approved=True, scope="all_project")
        assert not (await policy.await_approval(tool, call_id="denied", arguments={})).approved
        assert (await policy.await_approval(tool, call_id="approved", arguments={})).approved
        with anyio.fail_after(1):
            assert (await policy.await_approval(tool, call_id="waiting", arguments={})).approved
        assert not policy._pending

    anyio.run(scenario)


def test_save_failure_denies_yolo_without_granting_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("wisp.rpc.host.save_permission_mode", fail)

    async def scenario() -> None:
        policy = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval(), project_path=tmp_path)
        tool = BashTool()
        policy.prepare_approval(tool, call_id="call", arguments={})
        assert policy.resolve_approval(call_id="call", approved=True, scope="all_project")
        decision = await policy.await_approval(tool, call_id="call", arguments={})
        assert not decision.approved
        assert "Could not save" in (decision.reason or "")
        assert not policy.approves(tool)
        assert load_permission_mode(tmp_path) is None

    anyio.run(scenario)


def test_explicit_setting_can_revoke_startup_yes(tmp_path: Path) -> None:
    policy = RpcToolApprovalPolicy(ToolApprovalPolicy.approve_all(), project_path=tmp_path)
    assert policy.approves(BashTool())
    assert load_permission_mode(tmp_path) is None
    policy.set_permissions("ask")
    assert not policy.approves(BashTool())
    assert load_permission_mode(tmp_path) == "ask"


@pytest.mark.parametrize("scope", ["tool_session", "all_session"])
def test_session_reset_expires_temporary_grants(tmp_path: Path, scope: str) -> None:
    from typing import cast

    from wisp.rpc.commands import ApprovalScope

    policy = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval(), project_path=tmp_path)
    policy.prepare_approval(WriteTool(), call_id="grant", arguments={})
    assert policy.resolve_approval(call_id="grant", approved=True, scope=cast(ApprovalScope, scope))
    assert policy.approves(WriteTool())
    policy.reset_session_grants()
    assert not policy.approves(WriteTool())
    assert not policy.approves(BashTool())
    policy.set_permissions("yolo")
    policy.reset_session_grants()
    assert policy.approves(BashTool())
    policy.set_permissions("ask")
    policy.reset_session_grants()
    assert not policy.approves(BashTool())


@pytest.mark.parametrize("outcome", ["busy", "save_failure", "saved"])
def test_rpc_reports_saved_permissions_only_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from tests.support.rpc import build_rpc_executor_fixture
    from wisp.events import RpcCommandFinished, RpcPermissionsReported
    from wisp.rpc.commands import ParsedRpcCommand, SetPermissionsCommand
    from wisp.rpc.coordinator import _RpcRunningCommand

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        policy = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval(), project_path=tmp_path)
        if outcome == "save_failure":

            def fail(*_args: object) -> None:
                raise OSError("disk failure")

            monkeypatch.setattr(permissions.os, "replace", fail)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            executor.approval_policy = policy
            running = (
                _RpcRunningCommand("prompt", "prompt", anyio.CancelScope())
                if outcome == "busy"
                else None
            )
            result = await executor.dispatch_parsed(
                ParsedRpcCommand.from_known(SetPermissionsCommand(id="save", mode="yolo")),
                running,
            )
            assert result.running_command is running
        reports = [event for event in fixture.events if isinstance(event, RpcPermissionsReported)]
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert len(finished) == 1
        assert finished[0].ok == (outcome == "saved")
        assert bool(reports) == (outcome == "saved")
        assert policy.approves(BashTool()) == (outcome == "saved")
        assert load_permission_mode(tmp_path) == ("yolo" if outcome == "saved" else None)
        if reports:
            assert reports[0].permissions.saved_mode == "yolo"

    anyio.run(scenario)
