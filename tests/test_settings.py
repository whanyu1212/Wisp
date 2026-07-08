"""Tests for the layered settings resolver."""

from __future__ import annotations

import json
from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch

from wisp.config import WispConfig
from wisp.settings import DEFAULT_PROTECTED_PATHS, resolve_settings


def _write_settings(directory: Path, **values: object) -> None:
    """Write a ``.wisp/settings.json`` under ``directory``."""

    wisp_dir = directory / ".wisp"
    wisp_dir.mkdir(parents=True, exist_ok=True)
    (wisp_dir / "settings.json").write_text(json.dumps(values), encoding="utf-8")


def test_resolve_settings_empty_when_no_files(tmp_path: Path) -> None:
    settings = resolve_settings(project_dir=tmp_path / "proj", home_dir=tmp_path / "home")

    assert settings.provider is None
    assert settings.model is None
    assert settings.protected_paths is None


def test_project_settings_override_user_settings(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "proj"
    _write_settings(home, provider="user-provider", model="user-model")
    _write_settings(project, provider="project-provider")

    settings = resolve_settings(project_dir=project, home_dir=home)

    # Project wins where it sets a key; user fills the rest.
    assert settings.provider == "project-provider"
    assert settings.model == "user-model"


def test_user_settings_used_when_no_project_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_settings(home, provider="user-provider", session_dir="/tmp/user-sessions")

    settings = resolve_settings(project_dir=tmp_path / "proj", home_dir=home)

    assert settings.provider == "user-provider"
    assert settings.session_dir == "/tmp/user-sessions"


def test_malformed_settings_file_is_ignored(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    project = tmp_path / "proj"
    wisp_dir = project / ".wisp"
    wisp_dir.mkdir(parents=True)
    (wisp_dir / "settings.json").write_text("{not valid json", encoding="utf-8")

    settings = resolve_settings(project_dir=project, home_dir=tmp_path / "home")

    assert settings.provider is None  # falls through, no crash
    assert "malformed settings file" in capsys.readouterr().err


def test_non_object_settings_file_is_ignored(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    project = tmp_path / "proj"
    wisp_dir = project / ".wisp"
    wisp_dir.mkdir(parents=True)
    (wisp_dir / "settings.json").write_text("[1, 2, 3]", encoding="utf-8")

    settings = resolve_settings(project_dir=project, home_dir=tmp_path / "home")

    assert settings.provider is None
    assert "expected a JSON object" in capsys.readouterr().err


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _write_settings(project, provider="p", future_key="whatever")

    settings = resolve_settings(project_dir=project, home_dir=tmp_path / "home")

    assert settings.provider == "p"


def test_protected_paths_empty_list_is_preserved(tmp_path: Path) -> None:
    # An explicit empty list means "protect nothing" and must not fall through
    # to the user layer or the built-in default.
    home = tmp_path / "home"
    project = tmp_path / "proj"
    _write_settings(home, protected_paths=[".env"])
    _write_settings(project, protected_paths=[])

    settings = resolve_settings(project_dir=project, home_dir=home)

    assert settings.protected_paths == ()


def test_protected_paths_absent_falls_through(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_settings(home, protected_paths=["secret.txt"])

    settings = resolve_settings(project_dir=tmp_path / "proj", home_dir=home)

    assert settings.protected_paths == ("secret.txt",)


# --- Precedence through WispConfig.from_env (CLI > env > file > default) ---


def test_from_env_argument_beats_settings_file(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WISP_PROVIDER", raising=False)
    _write_settings(tmp_path, provider="file-provider")

    config = WispConfig.from_env(provider="arg-provider", load_env_file=False)

    assert config.provider == "arg-provider"


def test_from_env_env_beats_settings_file(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WISP_PROVIDER", "env-provider")
    _write_settings(tmp_path, provider="file-provider")

    config = WispConfig.from_env(load_env_file=False)

    assert config.provider == "env-provider"


def test_from_env_settings_file_beats_default(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WISP_PROVIDER", raising=False)
    monkeypatch.delenv("WISP_MODEL", raising=False)
    _write_settings(tmp_path, provider="file-provider", model="file-model")

    config = WispConfig.from_env(load_env_file=False)

    assert config.provider == "file-provider"
    assert config.model == "file-model"


def test_from_env_settings_file_sets_session_dir(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WISP_SESSION_DIR", raising=False)
    sessions = tmp_path / "custom-sessions"
    _write_settings(tmp_path, session_dir=str(sessions))

    config = WispConfig.from_env(load_env_file=False)

    assert config.session_dir == sessions


def test_from_env_env_session_dir_beats_settings_file(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    env_sessions = tmp_path / "env-sessions"
    monkeypatch.setenv("WISP_SESSION_DIR", str(env_sessions))
    _write_settings(tmp_path, session_dir=str(tmp_path / "file-sessions"))

    config = WispConfig.from_env(load_env_file=False)

    assert config.session_dir == env_sessions


def test_from_env_defaults_protected_paths(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    config = WispConfig.from_env(load_env_file=False)

    assert config.protected_paths == DEFAULT_PROTECTED_PATHS


def test_from_env_settings_can_disable_protected_paths(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_settings(tmp_path, protected_paths=[])

    config = WispConfig.from_env(load_env_file=False)

    assert config.protected_paths == ()
