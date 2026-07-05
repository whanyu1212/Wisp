from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch
from typer.testing import CliRunner

from wisp.auth.openai_codex import OpenAICodexLoginMethod
from wisp.auth.storage import OAuthCredential
from wisp.cli import app
from wisp.cli import auth as cli_auth_module


def test_auth_status_reports_no_credentials(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["auth", "status", "--auth-file", str(tmp_path / "auth.json")])

    assert result.exit_code == 0, result.output
    assert result.stdout == "no credentials configured\n"


def test_auth_login_status_and_logout_openai_codex(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    auth_file = tmp_path / "auth.json"

    async def fake_login(
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

    monkeypatch.setattr(cli_auth_module, "_login_openai_codex", fake_login)
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
