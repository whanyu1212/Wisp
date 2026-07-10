from __future__ import annotations

import json
from pathlib import Path

from pytest import MonkeyPatch
from typer.testing import CliRunner

from wisp.auth.openai_codex import OpenAICodexLoginMethod
from wisp.auth.storage import OAuthCredential
from wisp.cli import app
from wisp.cli import auth as cli_auth_module


async def _fake_oauth_login(
    method: OpenAICodexLoginMethod,
    *_args: object,
) -> OAuthCredential:
    assert method is OpenAICodexLoginMethod.device_code
    return OAuthCredential(
        access="access-token",
        refresh="refresh-token",
        expires=4_102_444_800_000,
        account_id="account-id",
    )


def _write_project_settings(project: Path, **settings: object) -> None:
    settings_dir = project / ".wisp"
    settings_dir.mkdir(parents=True, exist_ok=True)
    (settings_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


def _trust_project(project: Path, trust_file: Path, monkeypatch: MonkeyPatch) -> None:
    from wisp.trust import record_trust

    monkeypatch.setenv("WISP_TRUST_FILE", str(trust_file))
    record_trust(project, True, trust_path=trust_file)


def test_auth_status_reports_no_credentials(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["auth", "status", "--auth-file", str(tmp_path / "auth.json")])

    assert result.exit_code == 0, result.output
    assert result.stdout == "no credentials configured\n"


def test_auth_login_status_and_logout_openai_codex(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    auth_file = tmp_path / "auth.json"

    monkeypatch.setattr(cli_auth_module, "_login_openai_codex", _fake_oauth_login)
    runner = CliRunner()

    login = runner.invoke(
        app,
        [
            "auth",
            "login",
            "openai-codex",
            "--method",
            "device-code",
            "--auth-file",
            str(auth_file),
        ],
    )

    assert login.exit_code == 0, login.output
    assert "logged in: openai-codex" in login.stdout
    assert "access-token" not in login.stdout

    status = runner.invoke(app, ["auth", "status", "openai-codex", "--auth-file", str(auth_file)])

    assert status.exit_code == 0, status.output
    assert "openai-codex: oauth configured" in status.stdout
    assert "access-token" not in status.stdout

    logout = runner.invoke(app, ["auth", "logout", "openai-codex", "--auth-file", str(auth_file)])

    assert logout.exit_code == 0, logout.output
    assert logout.stdout == "logged out: openai-codex\n"


def test_auth_login_rejects_unknown_provider(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["auth", "login", "missing", "--auth-file", str(tmp_path / "auth.json")],
    )

    assert result.exit_code != 0
    assert "unsupported login provider" in result.output


def test_auth_commands_use_trusted_project_auth_path(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    project_auth = project / ".wisp" / "auth.json"
    _write_project_settings(project, auth_path=str(project_auth))
    _trust_project(project, tmp_path / "trust.json", monkeypatch)
    monkeypatch.chdir(project)
    monkeypatch.setattr(cli_auth_module, "_login_openai_codex", _fake_oauth_login)
    runner = CliRunner()

    login = runner.invoke(
        app,
        ["auth", "login", "openai-codex", "--method", "device-code"],
    )

    assert login.exit_code == 0, login.output
    assert f"credentials saved: {project_auth}" in login.stdout
    assert project_auth.exists()

    status = runner.invoke(app, ["auth", "status", "openai-codex"])

    assert status.exit_code == 0, status.output
    assert "openai-codex: oauth configured" in status.stdout

    logout = runner.invoke(app, ["auth", "logout", "openai-codex"])

    assert logout.exit_code == 0, logout.output
    assert logout.stdout == "logged out: openai-codex\n"


def test_auth_commands_use_trusted_root_settings_from_subdirectory(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    nested = project / "packages" / "app"
    nested.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname = 'example'\n", encoding="utf-8")
    project_auth = project / ".wisp" / "auth.json"
    _write_project_settings(project, auth_path=str(project_auth))
    _trust_project(project, tmp_path / "trust.json", monkeypatch)
    monkeypatch.chdir(nested)
    monkeypatch.setattr(cli_auth_module, "_login_openai_codex", _fake_oauth_login)

    result = CliRunner().invoke(
        app,
        ["auth", "login", "openai-codex", "--method", "device-code"],
    )

    assert result.exit_code == 0, result.output
    assert f"credentials saved: {project_auth}" in result.stdout
    assert project_auth.exists()


def test_auth_commands_ignore_untrusted_project_auth_path(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    project_auth = project / ".wisp" / "auth.json"
    _write_project_settings(project, auth_path=str(project_auth))
    monkeypatch.chdir(project)
    monkeypatch.setattr(cli_auth_module, "_login_openai_codex", _fake_oauth_login)
    default_auth = Path.home() / ".wisp" / "auth.json"

    result = CliRunner().invoke(
        app,
        ["auth", "login", "openai-codex", "--method", "device-code"],
    )

    assert result.exit_code == 0, result.output
    assert f"credentials saved: {default_auth}" in result.stdout
    assert default_auth.exists()
    assert not project_auth.exists()


def test_auth_file_option_overrides_trusted_project_auth_path(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    project_auth = project / ".wisp" / "auth.json"
    explicit_auth = tmp_path / "explicit-auth.json"
    _write_project_settings(project, auth_path=str(project_auth))
    _trust_project(project, tmp_path / "trust.json", monkeypatch)
    monkeypatch.chdir(project)
    monkeypatch.setattr(cli_auth_module, "_login_openai_codex", _fake_oauth_login)

    result = CliRunner().invoke(
        app,
        [
            "auth",
            "login",
            "openai-codex",
            "--method",
            "device-code",
            "--auth-file",
            str(explicit_auth),
        ],
    )

    assert result.exit_code == 0, result.output
    assert f"credentials saved: {explicit_auth}" in result.stdout
    assert explicit_auth.exists()
    assert not project_auth.exists()
