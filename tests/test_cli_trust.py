"""Tests for CLI trust resolution (env override + text-mode prompt)."""

from __future__ import annotations

import os
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


def test_project_env_cannot_self_trust_via_wisp_trust(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # Security (review finding 1): a malicious repo's .env with WISP_TRUST=1 must
    # not trust the project. load_project_env strips trust-critical keys.
    from wisp.config import load_project_env

    monkeypatch.delenv("WISP_TRUST", raising=False)
    project = tmp_path / "evilrepo"
    project.mkdir()
    (project / ".env").write_text("WISP_TRUST=1\n", encoding="utf-8")
    monkeypatch.chdir(project)

    load_project_env()

    assert os.environ.get("WISP_TRUST") is None
    assert trust_override_from_env() is None


def test_project_env_cannot_redirect_trust_file(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    # Security (review finding 2): .env must not be able to point WISP_TRUST_FILE at
    # a project-local trust store.
    from wisp.config import load_project_env

    monkeypatch.delenv("WISP_TRUST_FILE", raising=False)
    project = tmp_path / "evilrepo"
    project.mkdir()
    (project / ".env").write_text("WISP_TRUST_FILE=.wisp/trust.json\n", encoding="utf-8")
    monkeypatch.chdir(project)

    load_project_env()

    assert os.environ.get("WISP_TRUST_FILE") is None


def test_real_env_trust_survives_project_env_load(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    # A genuine process-environment WISP_TRUST is preserved; only .env is neutered.
    from wisp.config import load_project_env

    monkeypatch.setenv("WISP_TRUST", "1")
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".env").write_text("WISP_TRUST=0\n", encoding="utf-8")  # .env tries to flip it
    monkeypatch.chdir(project)

    load_project_env()

    # The real env value wins; .env cannot override it.
    assert os.environ.get("WISP_TRUST") == "1"
