"""YOLO and trust are never kept for the home folder or a folder above it (HY-252).

The test home folder is a pytest temporary directory (see ``tests/conftest.py``),
so these tests place projects at or above it to exercise the broad-folder rules.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import anyio
import pytest
from typer.testing import CliRunner

import wisp.coding.prompt.project_context as project_context_module
from tests.support.cli import CliRunner as RpcCliRunner
from tests.support.cli import DangerTool, ToolCallingProvider, app
from wisp.coding import CodingSession
from wisp.coding.prompt import resolve_project_context_root
from wisp.events import ToolApprovalRequested
from wisp.rpc.host import RpcToolApprovalPolicy, RpcTrustGate, _broad_root_notices
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.builtin import BashTool, WriteTool
from wisp.trust.flow import resolve_trust
from wisp.trust.permissions import (
    BroadRootYoloError,
    _preference_path,
    ignored_broad_root_yolo,
    load_permission_mode,
    permissions_directory,
    save_permission_mode,
)
from wisp.trust.records import (
    BroadRootTrustError,
    forget_trust,
    ignored_broad_root_trust,
    is_trusted,
    record_trust,
)
from wisp.trust.scope import is_broad_root


@pytest.fixture
def home() -> Path:
    return Path.home()


def _saved_permission_files() -> list[Path]:
    directory = permissions_directory()
    return sorted(directory.iterdir()) if directory.exists() else []


def _plant_yolo_record(project: Path) -> None:
    """Write a YOLO record the way releases before HY-252 could."""

    path = _preference_path(project)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps({"project": str(project.resolve()), "mode": "yolo"}))
    path.chmod(0o600)


# --- Which folders are broad ---


def test_every_spelling_of_home_and_its_ancestors_is_broad(home: Path, tmp_path: Path) -> None:
    link = tmp_path / "home-link"
    link.symlink_to(home, target_is_directory=True)

    for spelling in (
        home,
        Path(f"{home}/"),
        home / "notes" / "..",
        home / ".",
        link,
        home.parent,
        Path(home.anchor),
        Path(tempfile.gettempdir()),
    ):
        assert is_broad_root(spelling), spelling


def test_folders_below_home_are_not_broad(home: Path, tmp_path: Path) -> None:
    project = home / "code" / "app"
    project.mkdir(parents=True)
    link = tmp_path / "app-link"
    link.symlink_to(project, target_is_directory=True)

    assert not is_broad_root(project)
    assert not is_broad_root(link)
    assert not is_broad_root(tmp_path)


def test_a_symlinked_home_folder_matches_its_target(tmp_path: Path) -> None:
    real = tmp_path / "real-home"
    real.mkdir()
    link = tmp_path / "link-home"
    link.symlink_to(real, target_is_directory=True)

    assert is_broad_root(real, home=link)
    assert is_broad_root(link, home=real)
    assert not is_broad_root(real / "project", home=link)


def test_another_letter_case_of_home_is_broad(home: Path, tmp_path: Path) -> None:
    variant = Path(str(home).swapcase())
    if not variant.exists():
        pytest.skip("the file system is case-sensitive")
    trust_file = tmp_path / "trust.json"

    assert is_broad_root(variant)
    with pytest.raises(BroadRootTrustError):
        record_trust(variant, True, trust_path=trust_file)
    with pytest.raises(BroadRootYoloError):
        save_permission_mode(variant, "yolo")
    assert not trust_file.exists()
    assert _saved_permission_files() == []


# --- Saved permissions ---


@pytest.mark.parametrize("above", [0, 1])
def test_yolo_cannot_be_saved_for_home_or_above(home: Path, above: int) -> None:
    folder = home.parents[above - 1] if above else home

    with pytest.raises(BroadRootYoloError):
        save_permission_mode(folder, "yolo")

    assert _saved_permission_files() == []
    save_permission_mode(folder, "ask")
    assert load_permission_mode(folder) == "ask"


def test_a_yolo_record_from_an_earlier_release_is_ignored_not_deleted(home: Path) -> None:
    _plant_yolo_record(home)

    assert load_permission_mode(home) is None
    assert ignored_broad_root_yolo(home)
    assert _preference_path(home).exists()
    assert not RpcToolApprovalPolicy(
        ToolApprovalPolicy.require_approval(), project_path=home
    ).approves(BashTool())


def test_yolo_is_still_saved_for_a_project_below_home(home: Path) -> None:
    project = home / "code" / "app"
    project.mkdir(parents=True)

    save_permission_mode(project, "yolo")

    assert load_permission_mode(project) == "yolo"
    assert not ignored_broad_root_yolo(project)


def test_yolo_for_the_project_in_home_lasts_for_the_session_only(home: Path) -> None:
    async def scenario() -> None:
        policy = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval(), project_path=home)
        tool = BashTool()
        policy.prepare_approval(tool, call_id="call", arguments={})
        assert policy.resolve_approval(call_id="call", approved=True, scope="all_project")
        decision = await policy.await_approval(tool, call_id="call", arguments={})
        assert decision.approved
        assert policy.approves(tool)
        assert policy.approves(WriteTool())

        snapshot = policy.permissions_snapshot()
        assert snapshot.mode == "yolo"
        assert snapshot.saved_mode is None
        assert snapshot.session_only_yolo

        # Nothing was saved, so a new run asks again, and a new session clears it.
        assert _saved_permission_files() == []
        fresh = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval(), project_path=home)
        assert not fresh.approves(tool)
        policy.reset_session_grants()
        assert not policy.approves(tool)

    anyio.run(scenario)


def test_permissions_command_in_home_applies_yolo_without_saving(home: Path) -> None:
    _plant_yolo_record(home)
    policy = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval(), project_path=home)

    policy.set_permissions("yolo")

    assert policy.approves(BashTool())
    assert policy.permissions_snapshot().saved_mode is None
    # Choosing Ask clears the session's YOLO and replaces the stale record.
    policy.set_permissions("ask")
    assert not policy.approves(BashTool())
    assert load_permission_mode(home) == "ask"
    assert not ignored_broad_root_yolo(home)


def test_approval_requests_in_home_offer_session_only_yolo(home: Path, tmp_path: Path) -> None:
    def requested(project: Path) -> ToolApprovalRequested:
        tools = ToolRegistry()
        tools.register(DangerTool())
        policy = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval(), project_path=project)

        async def run() -> ToolApprovalRequested:
            agent = CodingSession(
                provider=ToolCallingProvider(),
                sessions=JsonlSessionStore(tmp_path / "sessions"),
                tool_registry=tools,
                tool_approval_policy=policy,
            )
            request: ToolApprovalRequested | None = None
            async for event in agent.run("use tool"):
                if isinstance(event, ToolApprovalRequested):
                    request = event
                    policy.resolve_approval(call_id=event.call_id, approved=False)
            assert request is not None
            return request

        return anyio.run(run)

    project = home / "code" / "app"
    project.mkdir(parents=True)
    assert requested(home).session_only_yolo
    assert not requested(project).session_only_yolo


def test_startup_notices_name_the_folder_and_an_ignored_record(home: Path) -> None:
    project = home / "code" / "app"
    project.mkdir(parents=True)

    assert _broad_root_notices(project) == ()
    (notice,) = _broad_root_notices(home)
    assert str(home) in notice
    assert "this session only" in notice

    _plant_yolo_record(home)
    notices = _broad_root_notices(home)
    assert len(notices) == 2
    assert "no longer applies" in notices[1]


def test_startup_notices_report_an_ignored_trust_record(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trust_file = tmp_path / "trust.json"
    monkeypatch.setenv("WISP_TRUST_FILE", str(trust_file))
    trust_file.write_text(json.dumps({str(home.resolve()): {"trusted": True}}))

    notices = _broad_root_notices(home)

    assert len(notices) == 2
    assert "wisp trust forget" in notices[1]


def test_rpc_in_home_reports_session_only_yolo_and_saves_nothing(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(home)
    commands = [
        {"type": "set_permissions", "id": "enable", "mode": "yolo"},
        {"type": "get_permissions", "id": "read"},
    ]
    result = RpcCliRunner().invoke(
        app,
        ["--mode", "rpc", "--provider", "fake", "--session-dir", str(tmp_path / "sessions")],
        input="".join(json.dumps(command) + "\n" for command in commands),
    )

    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.stdout.splitlines()]
    errors = [record["message"] for record in records if record["type"] == "error"]
    assert any("your home folder" in message for message in errors), errors
    assert all(record.get("ok", True) for record in records), records
    reports = [record["permissions"] for record in records if record["type"] == "rpc.permissions"]
    assert [report["mode"] for report in reports] == ["yolo", "yolo"]
    assert all(report["saved_mode"] is None for report in reports)
    assert all(report["session_only_yolo"] for report in reports)
    assert _saved_permission_files() == []


# --- Trust ---


@pytest.mark.parametrize("above", [0, 1])
def test_trust_cannot_be_recorded_for_home_or_above(home: Path, tmp_path: Path, above: int) -> None:
    folder = home.parents[above - 1] if above else home
    trust_file = tmp_path / "trust.json"

    for trusted in (True, False):
        with pytest.raises(BroadRootTrustError):
            record_trust(folder, trusted, trust_path=trust_file)

    assert not trust_file.exists()


def test_a_trust_record_from_an_earlier_release_is_ignored(home: Path, tmp_path: Path) -> None:
    trust_file = tmp_path / "trust.json"
    trust_file.write_text(json.dumps({str(home.resolve()): {"trusted": True}}))

    assert is_trusted(home, trust_path=trust_file) is None
    asked: list[Path] = []

    def prompter(project: Path) -> bool:
        asked.append(project)
        return True

    decision = resolve_trust(home, prompter=prompter, trust_path=trust_file)

    assert asked == [home]
    assert decision.trusted
    assert ignored_broad_root_trust(home, trust_path=trust_file)
    # The answer holds for this run only; the old record is left for the user.
    assert json.loads(trust_file.read_text()) == {str(home.resolve()): {"trusted": True}}
    forget_trust(home, trust_path=trust_file)
    assert json.loads(trust_file.read_text()) == {}


def test_rpc_trust_answer_in_home_is_not_recorded(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trust_file = tmp_path / "trust.json"
    monkeypatch.setenv("WISP_TRUST_FILE", str(trust_file))
    gate = RpcTrustGate(home, write_event=lambda _event: None)

    async def scenario() -> bool:
        decision: bool | None = None
        done = anyio.Event()

        async def resolve() -> None:
            nonlocal decision
            decision = await gate.resolve()
            done.set()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(resolve)
            with anyio.fail_after(2):
                while gate._pending is None:
                    await anyio.sleep(0)
            pending = gate._pending
            assert pending is not None
            assert gate.resolve_request(request_id=pending.request_id, trusted=True)
            with anyio.fail_after(2):
                await done.wait()
            task_group.cancel_scope.cancel()
        assert decision is not None
        return decision

    assert anyio.run(scenario) is True
    assert not trust_file.exists()


def test_trust_cli_refuses_to_record_home(home: Path, tmp_path: Path) -> None:
    env = {"WISP_TRUST_FILE": str(tmp_path / "trust.json")}
    runner = CliRunner()

    for command in ("allow", "revoke"):
        result = runner.invoke(app, ["trust", command, str(home)], env=env)
        assert result.exit_code == 1, result.output
        assert "WISP_TRUST=1" in result.stderr
        assert not (tmp_path / "trust.json").exists()

    status = runner.invoke(app, ["trust", "status", str(home)], env=env)
    assert status.exit_code == 0, status.output
    assert status.stdout.startswith("asked each session")


# --- Project root discovery ---


def test_a_marker_in_home_does_not_make_home_the_project(home: Path) -> None:
    (home / "package.json").write_text("{}")
    (home / "AGENTS.md").write_text("home notes\n")
    notes = home / "notes" / "2026"
    notes.mkdir(parents=True)

    assert resolve_project_context_root(notes) == notes.resolve()
    assert resolve_project_context_root(home) == home.resolve()


def test_a_marker_below_home_still_marks_the_project(home: Path) -> None:
    (home / "package.json").write_text("{}")
    project = home / "code" / "app"
    nested = project / "src"
    nested.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname = 'app'\n")

    assert resolve_project_context_root(nested) == project.resolve()


def test_a_git_repository_at_home_does_not_make_home_the_project(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes = home / "notes"
    notes.mkdir()

    def dotfiles_repo(_cwd: Path, *args: str) -> str | None:
        return str(home) if args == ("rev-parse", "--show-toplevel") else None

    monkeypatch.setattr(project_context_module, "_run_git", dotfiles_repo)

    assert resolve_project_context_root(notes) == notes.resolve()


@pytest.mark.skipif(os.name != "posix", reason="uses a real git repository")
def test_a_real_git_repository_below_home_is_still_the_project(home: Path) -> None:
    repo = home / "code" / "repo"
    nested = repo / "src"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    assert resolve_project_context_root(nested) == repo.resolve()
