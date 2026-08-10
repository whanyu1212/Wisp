from __future__ import annotations

from pytest import MonkeyPatch
from typer.testing import CliRunner

from wisp.cli import app
from wisp.cli import update as update_module
from wisp.update_check import (
    UpdateAvailable,
    UpdateCheckError,
    UpdateStatus,
)


def _patch_status(monkeypatch: MonkeyPatch, status: UpdateStatus) -> None:
    async def get_status() -> UpdateStatus:
        return status

    monkeypatch.setattr(update_module, "get_update_status", get_status)


def test_update_check_reports_available_release_without_installing(
    monkeypatch: MonkeyPatch,
) -> None:
    _patch_status(monkeypatch, UpdateStatus("1.0.0", "1.2.0"))

    result = CliRunner().invoke(app, ["update", "--check"])

    assert result.exit_code == 0
    assert "Wisp 1.2.0 is available (current 1.0.0)." in result.stdout
    assert "Update with: wisp update" in result.stdout


def test_update_reports_current_release(monkeypatch: MonkeyPatch) -> None:
    _patch_status(monkeypatch, UpdateStatus("1.2.0", "1.2.0"))

    result = CliRunner().invoke(app, ["update"])

    assert result.exit_code == 0
    assert result.stdout == "Wisp 1.2.0 is up to date.\n"


def test_update_requires_confirmation_unless_yes(monkeypatch: MonkeyPatch) -> None:
    _patch_status(monkeypatch, UpdateStatus("1.0.0", "1.2.0"))
    installed: list[UpdateAvailable] = []

    async def install(update: UpdateAvailable) -> None:
        installed.append(update)

    monkeypatch.setattr(update_module, "install_update", install)
    runner = CliRunner()

    declined = runner.invoke(app, ["update"], input="n\n")
    accepted = runner.invoke(app, ["update", "--yes"])

    assert declined.exit_code == 0
    assert "Update cancelled." in declined.stdout
    assert accepted.exit_code == 0
    assert "Updated Wisp to 1.2.0. Restart Wisp" in accepted.stdout
    assert [update.latest_version for update in installed] == ["1.2.0"]


def test_update_check_failure_is_actionable(monkeypatch: MonkeyPatch) -> None:
    async def fail() -> UpdateStatus:
        raise UpdateCheckError("offline")

    monkeypatch.setattr(update_module, "get_update_status", fail)

    result = CliRunner().invoke(app, ["update", "--check"])

    assert result.exit_code == 1
    assert "Update check failed: offline" in result.stderr
