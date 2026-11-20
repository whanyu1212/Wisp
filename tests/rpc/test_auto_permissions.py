"""Auto consent is project-local, explicitly edit-only, and revocable."""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

from tests.support.rpc import RecordingEventWriter
from wisp.config.runtime import WispConfig
from wisp.events import PermissionMode, SessionDirectoryChanged
from wisp.messages import Message
from wisp.rpc.host import InProcessOptions, RpcHost, RpcToolApprovalPolicy, build_session_runtime
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.approval import ToolApprovalPolicy
from wisp.trust.permissions import (
    BroadRootYoloError,
    _preference_path,
    load_permission_mode,
    save_permission_mode,
)


def test_auto_is_saved_with_an_explicit_scope_and_loaded(tmp_path: Path) -> None:
    policy = RpcToolApprovalPolicy(ToolApprovalPolicy.approve_all(), project_path=tmp_path)
    policy.approved_tools = frozenset({"bash"})
    policy.set_permissions("auto")
    snapshot = policy.permissions_snapshot()
    assert snapshot.mode == snapshot.saved_mode == "auto"
    assert not policy.approved_safety and not policy.approved_tools
    assert json.loads(_preference_path(tmp_path).read_text())["auto_scope"] == "workspace_edit_v1"
    fresh = RpcToolApprovalPolicy(ToolApprovalPolicy(), project_path=tmp_path)
    assert fresh.mode == "auto"
    fresh.reset_session_grants()
    assert fresh.permissions_snapshot().mode == "auto"


@pytest.mark.parametrize("scope", [None, "shell", "workspace_edit_v2"])
def test_auto_records_without_exact_edit_consent_fail_closed(
    tmp_path: Path, scope: str | None
) -> None:
    save_permission_mode(tmp_path, "auto")
    path = _preference_path(tmp_path)
    record = json.loads(path.read_text())
    record["auto_scope"] = scope
    path.write_text(json.dumps(record))
    assert load_permission_mode(tmp_path) is None


def test_command_review_requires_distinct_saved_consent(tmp_path: Path) -> None:
    save_permission_mode(tmp_path, "auto")
    path = _preference_path(tmp_path)
    record = json.loads(path.read_text())
    record["mode"] = "auto_commands"
    path.write_text(json.dumps(record))
    assert load_permission_mode(tmp_path) is None
    save_permission_mode(tmp_path, "auto_commands")
    record = json.loads(path.read_text())
    assert record["auto_scope"] == "sandbox_commands_v1"
    policy = RpcToolApprovalPolicy(ToolApprovalPolicy(), project_path=tmp_path)
    assert policy.permissions_snapshot().mode == "auto_commands"
    assert not policy.approved_tools and not policy.approved_safety
    generation = policy.generation
    policy.set_permissions("ask")
    assert policy.generation > generation and policy.mode == "ask"
    assert load_permission_mode(tmp_path) == "ask"


@pytest.mark.parametrize(
    "scope", [None, "workspace_edit_v1", "command_review_v1", "command_review_v2"]
)
def test_command_review_records_fail_closed_without_exact_scope(
    tmp_path: Path, scope: str | None
) -> None:
    save_permission_mode(tmp_path, "auto_commands")
    path = _preference_path(tmp_path)
    record = json.loads(path.read_text())
    record["auto_scope"] = scope
    path.write_text(json.dumps(record))
    assert load_permission_mode(tmp_path) is None


def test_command_review_in_home_is_not_persisted_or_restored_on_reset() -> None:
    policy = RpcToolApprovalPolicy(ToolApprovalPolicy.approve_all(), project_path=Path.home())
    policy.set_permissions("auto_commands")
    assert policy.mode == "auto_commands" and policy.permissions_snapshot().session_only_yolo
    assert not _preference_path(Path.home()).exists()
    policy.reset_session_grants()
    assert policy.mode == "ask" and not policy.approved_safety
    with pytest.raises(BroadRootYoloError):
        save_permission_mode(Path.home(), "auto_commands")


def test_project_switch_and_ask_revoke_auto(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    save_permission_mode(first, "auto")
    policy = RpcToolApprovalPolicy(ToolApprovalPolicy(), project_path=first)
    policy.approved_tools = frozenset({"bash"})
    policy.set_project(second)
    assert policy.mode == "ask" and not policy.approved_tools
    policy.set_project(first)
    assert policy.mode == "auto"
    policy.set_permissions("ask")
    policy.set_project(second)
    policy.set_project(first)
    assert policy.mode == "ask"


def test_broad_root_auto_is_session_only() -> None:
    home = Path.home()
    policy = RpcToolApprovalPolicy(ToolApprovalPolicy(), project_path=home)
    policy.set_permissions("auto")
    assert policy.permissions_snapshot().mode == "auto"
    assert policy.permissions_snapshot().session_only_yolo
    assert policy.permissions_snapshot().saved_mode is None
    assert not _preference_path(home).exists()
    assert RpcToolApprovalPolicy(ToolApprovalPolicy(), project_path=home).mode == "ask"
    policy.reset_session_grants()
    assert policy.mode == "ask"
    with pytest.raises(BroadRootYoloError):
        save_permission_mode(home, "auto")


def test_broad_root_auto_replaces_startup_yolo_even_after_session_reset(tmp_path: Path) -> None:
    policy = RpcToolApprovalPolicy(ToolApprovalPolicy.approve_all(), project_path=Path.home())
    policy.set_permissions("auto")
    policy.reset_session_grants()
    assert policy.permissions_snapshot().mode == "ask"
    assert not policy.approved_tools and not policy.approved_safety
    policy.set_project(tmp_path)
    assert policy.permissions_snapshot().mode == "ask"


def test_failed_save_does_not_change_current_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wisp.rpc.host as host

    policy = RpcToolApprovalPolicy(ToolApprovalPolicy(), project_path=tmp_path)

    def fail(project: Path, mode: object) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(host, "save_permission_mode", fail)
    with pytest.raises(OSError, match="disk unavailable"):
        policy.set_permissions("auto")
    assert policy.mode == "ask"


@pytest.mark.parametrize("other_project", [False, True])
def test_read_grant_stays_in_launch_project(tmp_path: Path, other_project: bool) -> None:
    launch = tmp_path / "launch"
    resumed = tmp_path / "elsewhere" if other_project else launch / "nested"
    fixtures = tmp_path / "fixtures"
    for directory in (launch, resumed, fixtures):
        directory.mkdir()
    config = WispConfig(provider="fake", session_dir=tmp_path / "sessions")

    async def scenario() -> None:
        session = JsonlSessionStore(config.session_dir).create()
        await session.append_message(Message(role="user", content="first"))
        await session.append_event(
            SessionDirectoryChanged(session_id=session.session_id, previous_cwd=launch, cwd=resumed)
        )
        writer = RecordingEventWriter()
        runtime = await build_session_runtime(config, connect_mcp=False)
        try:
            host = await RpcHost.create(
                config,
                runtime,
                options=InProcessOptions(
                    cwd=launch,
                    project_context_root=launch,
                    resume=session.session_id,
                    auto_command_read_dir=fixtures,
                ),
                write_event=writer,
                render_events=writer.render_events,
                wake_by_default=False,
            )
            assert host.agent.tool_context.cwd == resumed
            assert host.approval_policy.auto_command_read_dir == (
                None if other_project else fixtures
            )
        finally:
            await runtime.aclose()

    anyio.run(scenario)


@pytest.mark.parametrize("exit_mode", ["ask", "auto", "yolo"])
def test_leaving_auto_commands_revokes_read_dir(tmp_path: Path, exit_mode: PermissionMode) -> None:
    policy = RpcToolApprovalPolicy(ToolApprovalPolicy(), project_path=tmp_path / "project")
    fixtures = tmp_path / "fixtures"
    policy.auto_command_read_dir = fixtures

    policy.set_permissions("auto_commands")
    assert policy.auto_command_read_dir == fixtures

    generation = policy.generation
    policy.set_permissions(exit_mode)
    assert policy.generation > generation
    policy.set_permissions("auto_commands")
    assert policy.auto_command_read_dir is None


@pytest.mark.parametrize("change_project", [False, True])
def test_scope_change_revokes_read_dir(tmp_path: Path, change_project: bool) -> None:
    project = tmp_path / "project"
    policy = RpcToolApprovalPolicy(ToolApprovalPolicy(), project_path=project)
    policy.auto_command_read_dir = tmp_path / "fixtures"
    generation = policy.generation

    if change_project:
        policy.set_project(tmp_path / "other")
        policy.set_project(project)
    else:
        policy.reset_session_grants()
    assert policy.generation > generation
    policy.set_permissions("auto_commands")

    assert policy.auto_command_read_dir is None
