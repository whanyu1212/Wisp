"""Auto consent is project-local, explicitly edit-only, and revocable."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wisp.rpc.host import RpcToolApprovalPolicy
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
