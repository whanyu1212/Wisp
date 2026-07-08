"""Tests for CLI trust resolution (env override + text-mode prompt)."""

from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch

from wisp.cli.trust import resolve_cli_trust, trust_override_from_env
from wisp.trust import is_trusted, record_trust


def test_env_override_trusted(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("WISP_TRUST", "1")
    assert trust_override_from_env() is True
    monkeypatch.setenv("WISP_TRUST", "yes")
    assert trust_override_from_env() is True


def test_env_override_untrusted(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("WISP_TRUST", "0")
    assert trust_override_from_env() is False
    monkeypatch.setenv("WISP_TRUST", "off")
    assert trust_override_from_env() is False


def test_env_override_absent_or_unrecognized(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("WISP_TRUST", raising=False)
    assert trust_override_from_env() is None
    monkeypatch.setenv("WISP_TRUST", "maybe")
    assert trust_override_from_env() is None


def test_cli_trust_honors_env_override_over_prompt(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    trust_file = tmp_path / "trust.json"
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setenv("WISP_TRUST", "1")
    # Even if stdin looked interactive, the env override wins and never prompts.
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    decision = resolve_cli_trust(project, trust_path=trust_file)

    assert decision.trusted is True
    # An env override is not persisted (it is a per-run directive).
    assert is_trusted(project, trust_path=trust_file) is None


def test_cli_trust_non_interactive_defaults_untrusted(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    trust_file = tmp_path / "trust.json"
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.delenv("WISP_TRUST", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    decision = resolve_cli_trust(project, trust_path=trust_file)

    assert decision.trusted is False
    assert is_trusted(project, trust_path=trust_file) is None


def test_cli_trust_uses_stored_decision(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    trust_file = tmp_path / "trust.json"
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.delenv("WISP_TRUST", raising=False)
    record_trust(project, True, trust_path=trust_file)

    decision = resolve_cli_trust(project, trust_path=trust_file)

    assert decision.trusted is True
