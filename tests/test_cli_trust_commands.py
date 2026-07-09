"""Tests for user-facing project trust management commands."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from wisp.cli import app
from wisp.trust import is_trusted


def _env(trust_file: Path) -> dict[str, str]:
    return {"WISP_TRUST_FILE": str(trust_file)}


def _canonical(path: Path) -> str:
    return path.expanduser().resolve(strict=False).as_posix()


def test_trust_status_reports_undecided_for_current_directory(tmp_path: Path) -> None:
    trust_file = tmp_path / "trust.json"

    result = CliRunner().invoke(app, ["trust", "status"], env=_env(trust_file))

    assert result.exit_code == 0, result.output
    assert result.stdout == f"undecided: {_canonical(Path('.'))}\n"


def test_trust_allow_records_trusted_and_status_reports_it(tmp_path: Path) -> None:
    trust_file = tmp_path / "trust.json"
    project = tmp_path / "proj"
    project.mkdir()
    runner = CliRunner()

    allow = runner.invoke(app, ["trust", "allow", str(project)], env=_env(trust_file))

    assert allow.exit_code == 0, allow.output
    assert allow.stdout == f"trusted: {_canonical(project)}\n"
    assert is_trusted(project, trust_path=trust_file) is True

    status = runner.invoke(app, ["trust", "status", str(project)], env=_env(trust_file))

    assert status.exit_code == 0, status.output
    assert status.stdout == f"trusted: {_canonical(project)}\n"


def test_trust_revoke_records_untrusted_and_status_reports_it(tmp_path: Path) -> None:
    trust_file = tmp_path / "trust.json"
    project = tmp_path / "proj"
    project.mkdir()
    runner = CliRunner()

    allow = runner.invoke(app, ["trust", "allow", str(project)], env=_env(trust_file))
    revoke = runner.invoke(app, ["trust", "revoke", str(project)], env=_env(trust_file))

    assert allow.exit_code == 0, allow.output
    assert revoke.exit_code == 0, revoke.output
    assert revoke.stdout == f"untrusted: {_canonical(project)}\n"
    assert is_trusted(project, trust_path=trust_file) is False

    status = runner.invoke(app, ["trust", "status", str(project)], env=_env(trust_file))

    assert status.exit_code == 0, status.output
    assert status.stdout == f"untrusted: {_canonical(project)}\n"


def test_trust_forget_removes_record_and_status_returns_undecided(tmp_path: Path) -> None:
    trust_file = tmp_path / "trust.json"
    project = tmp_path / "proj"
    project.mkdir()
    runner = CliRunner()

    allow = runner.invoke(app, ["trust", "allow", str(project)], env=_env(trust_file))
    forget = runner.invoke(app, ["trust", "forget", str(project)], env=_env(trust_file))

    assert allow.exit_code == 0, allow.output
    assert forget.exit_code == 0, forget.output
    assert forget.stdout == f"forgot trust decision: {_canonical(project)}\n"
    assert is_trusted(project, trust_path=trust_file) is None

    status = runner.invoke(app, ["trust", "status", str(project)], env=_env(trust_file))

    assert status.exit_code == 0, status.output
    assert status.stdout == f"undecided: {_canonical(project)}\n"


def test_trust_forget_without_record_succeeds(tmp_path: Path) -> None:
    trust_file = tmp_path / "trust.json"
    project = tmp_path / "proj"
    project.mkdir()

    result = CliRunner().invoke(app, ["trust", "forget", str(project)], env=_env(trust_file))

    assert result.exit_code == 0, result.output
    assert result.stdout == f"no trust decision: {_canonical(project)}\n"
    assert is_trusted(project, trust_path=trust_file) is None
