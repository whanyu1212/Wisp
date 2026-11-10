"""The RPC approval policy when the session moves into another project."""

from __future__ import annotations

from pathlib import Path

from wisp.rpc.host import RpcToolApprovalPolicy
from wisp.tools.approval import ToolApprovalPolicy
from wisp.trust.permissions import save_permission_mode


def test_new_project_loads_its_mode(tmp_path: Path) -> None:
    ask, yolo = tmp_path / "ask", tmp_path / "yolo"
    save_permission_mode(yolo, "yolo")
    policy = RpcToolApprovalPolicy(ToolApprovalPolicy(), project_path=ask)
    policy.approved_tools = policy.approved_tools | {"bash"}

    policy.set_project(yolo)

    snapshot = policy.permissions_snapshot()
    assert (snapshot.mode, snapshot.saved_mode, snapshot.project_path) == ("yolo", "yolo", yolo)
    # The old project's temporary grant does not follow the session.
    assert "bash" not in policy.approved_tools

    policy.set_project(ask)

    assert policy.permissions_snapshot().mode == "ask"


def test_startup_grants_survive_move(tmp_path: Path) -> None:
    """Grants the host started with, such as --yes, apply in every project."""

    policy = RpcToolApprovalPolicy(
        ToolApprovalPolicy(approved_safety=frozenset({"mutating"})), project_path=tmp_path / "a"
    )

    policy.set_project(tmp_path / "b")

    assert "mutating" in policy.approved_safety


def test_explicit_ask_outlasts_move(tmp_path: Path) -> None:
    """Choosing Ask after a --yes start keeps Ask in the next project too."""

    policy = RpcToolApprovalPolicy(ToolApprovalPolicy.approve_all(), project_path=tmp_path / "a")
    policy.set_permissions("ask")

    policy.set_project(tmp_path / "b")

    assert policy.permissions_snapshot().mode == "ask"
    assert not policy.approved_safety & {"mutating", "command"}
