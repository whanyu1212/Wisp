"""Tests for the saved browser profile command."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from wisp.cli import app
from wisp.coding.browser_control import BrowserControlError


def test_clear_uses_user_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    profiles: list[Path] = []
    monkeypatch.setattr("wisp.cli.browser.clear_browser_profile", profiles.append)

    result = CliRunner().invoke(app, ["browser", "clear", "--yes"])

    assert result.exit_code == 0, result.output
    assert profiles == [Path.home() / ".wisp" / "browser-profile"]


def test_clear_requires_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    profiles: list[Path] = []
    monkeypatch.setattr("wisp.cli.browser.clear_browser_profile", profiles.append)

    result = CliRunner().invoke(app, ["browser", "clear"])

    assert result.exit_code == 1
    assert "--yes" in result.output
    assert profiles == []


@pytest.mark.parametrize("answer", ["n", "y"])
def test_clear_respects_terminal_confirmation(monkeypatch: pytest.MonkeyPatch, answer: str) -> None:
    profiles: list[Path] = []
    monkeypatch.setattr("wisp.cli.browser.clear_browser_profile", profiles.append)
    terminal = SimpleNamespace(isatty=lambda: True)
    monkeypatch.setattr("wisp.cli.browser.sys", SimpleNamespace(stdin=terminal))

    result = CliRunner().invoke(app, ["browser", "clear"], input=f"{answer}\n")

    assert "including site sign-ins" in result.output
    if answer == "y":
        assert result.exit_code == 0, result.output
        assert profiles == [Path.home() / ".wisp" / "browser-profile"]
    else:
        assert result.exit_code == 1
        assert profiles == []


def test_clear_reports_busy_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    def busy(path: Path) -> None:
        raise BrowserControlError("The saved browser profile is in use.")

    monkeypatch.setattr("wisp.cli.browser.clear_browser_profile", busy)

    result = CliRunner().invoke(app, ["browser", "clear", "--yes"])

    assert result.exit_code == 1
    assert "profile is in use" in result.stderr
