from __future__ import annotations

import json
from pathlib import Path

from pytest import MonkeyPatch
from typer.testing import CliRunner

from wisp.auth.storage import ApiKeyCredential, JsonAuthStore, OAuthCredential
from wisp.cli import app


def _oauth_credential() -> OAuthCredential:
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


def test_auth_status_and_logout_openai_codex(tmp_path: Path) -> None:
    auth_file = tmp_path / "auth.json"
    JsonAuthStore(auth_file).set("openai-codex", _oauth_credential())
    runner = CliRunner()

    status = runner.invoke(app, ["auth", "status", "openai-codex", "--auth-file", str(auth_file)])

    assert status.exit_code == 0, status.output
    assert "openai-codex: oauth configured" in status.stdout
    assert "access-token" not in status.stdout

    logout = runner.invoke(app, ["auth", "logout", "openai-codex", "--auth-file", str(auth_file)])

    assert logout.exit_code == 0, logout.output
    assert logout.stdout == "logged out: openai-codex\n"


def test_auth_help_omits_login_command() -> None:
    result = CliRunner().invoke(app, ["auth", "--help"])

    assert result.exit_code == 0, result.output
    assert "login" not in result.output
    assert "status" in result.output
    assert "logout" in result.output


def test_auth_commands_use_trusted_project_auth_path(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    project_auth = project / ".wisp" / "auth.json"
    _write_project_settings(project, auth_path=str(project_auth))
    _trust_project(project, tmp_path / "trust.json", monkeypatch)
    JsonAuthStore(project_auth).set("openai-codex", _oauth_credential())
    monkeypatch.chdir(project)
    runner = CliRunner()

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
    JsonAuthStore(project_auth).set("openai-codex", _oauth_credential())
    monkeypatch.chdir(nested)

    result = CliRunner().invoke(
        app,
        ["auth", "status", "openai-codex"],
    )

    assert result.exit_code == 0, result.output
    assert "openai-codex: oauth configured" in result.stdout


def test_auth_commands_ignore_untrusted_project_auth_path(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    project_auth = project / ".wisp" / "auth.json"
    _write_project_settings(project, auth_path=str(project_auth))
    JsonAuthStore(project_auth).set("openai-codex", _oauth_credential())
    monkeypatch.chdir(project)

    result = CliRunner().invoke(
        app,
        ["auth", "status", "openai-codex"],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "openai-codex: not logged in\n"


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
    JsonAuthStore(project_auth).set("openai-codex", _oauth_credential())
    JsonAuthStore(explicit_auth).set("openai-codex", ApiKeyCredential(key="explicit-key"))
    monkeypatch.chdir(project)

    result = CliRunner().invoke(
        app,
        [
            "auth",
            "status",
            "openai-codex",
            "--auth-file",
            str(explicit_auth),
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "openai-codex: api key configured\n"
