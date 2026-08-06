from __future__ import annotations

from pathlib import Path

import pytest
from pytest import MonkeyPatch

from wisp import config as config_module
from wisp.config import WispConfig, default_auth_path, default_session_dir
from wisp.settings import persist_user_model_selection


def test_config_defaults_to_default_provider(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("WISP_PROVIDER", raising=False)
    monkeypatch.delenv("WISP_MODEL", raising=False)
    monkeypatch.delenv("WISP_EFFORT", raising=False)

    config = WispConfig.from_env(session_dir=tmp_path)

    assert config.provider == config_module.DEFAULT_PROVIDER
    assert config.model is None
    assert config.effort is None
    assert config.context_reserve_tokens == 16_384
    assert config.auto_compaction_enabled is True
    assert config.session_dir == tmp_path
    assert config.auth_path == default_auth_path()


def test_config_defaults_session_dir_to_durable_home_path(monkeypatch: MonkeyPatch) -> None:
    # Sessions persist to ~/.wisp/sessions by default so transcripts survive runs
    # and can be resumed (no env override in effect).
    monkeypatch.delenv("WISP_SESSION_DIR", raising=False)

    session_dir = default_session_dir()

    assert session_dir == Path("~/.wisp/sessions").expanduser()
    assert session_dir.is_absolute()  # ~ expanded, not a literal "~"


def test_config_reads_session_dir_from_env(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    selected = tmp_path / "sessions"
    monkeypatch.setenv("WISP_SESSION_DIR", str(selected))

    config = WispConfig.from_env()

    assert config.session_dir == selected


def test_config_reads_auth_path_from_env(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    selected = tmp_path / "auth.json"
    monkeypatch.setenv("WISP_AUTH_FILE", str(selected))

    config = WispConfig.from_env()

    assert config.auth_path == selected


def test_config_reads_provider_and_model_from_env(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("WISP_PROVIDER", "openai")
    monkeypatch.setenv("WISP_MODEL", "gpt-5.5")

    config = WispConfig.from_env(session_dir=tmp_path)

    assert config.provider == "openai"
    assert config.model == "gpt-5.5"


def test_explicit_config_values_override_env(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("WISP_PROVIDER", "openai")
    monkeypatch.setenv("WISP_MODEL", "gpt-5.5")
    monkeypatch.setenv("WISP_EFFORT", "low")

    config = WispConfig.from_env(
        provider="fake",
        model="fake-model",
        effort="high",
        session_dir=tmp_path,
        auth_path=tmp_path / "auth.json",
    )

    assert config.provider == "fake"
    assert config.model == "fake-model"
    assert config.effort == "high"
    assert config.auth_path == tmp_path / "auth.json"


def test_config_reads_effort_from_env(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("WISP_EFFORT", "medium")

    config = WispConfig.from_env(session_dir=tmp_path)

    assert config.effort == "medium"


def test_config_reuses_persisted_tui_model_selection_on_restart(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    for name in ("WISP_PROVIDER", "WISP_MODEL", "WISP_EFFORT"):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    persist_user_model_selection("anthropic", "claude-opus-4-8", "high", home_dir=home)

    config = WispConfig.from_env(session_dir=tmp_path)

    assert config.provider == "anthropic"
    assert config.model == "claude-opus-4-8"
    assert config.effort == "high"


def test_model_only_user_setting_uses_default_provider(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    settings_dir = home / ".wisp"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.json").write_text('{"model": "gpt-custom"}', encoding="utf-8")
    monkeypatch.delenv("WISP_PROVIDER", raising=False)
    monkeypatch.delenv("WISP_MODEL", raising=False)

    config = WispConfig.from_env(session_dir=tmp_path)

    assert config.provider == config_module.DEFAULT_PROVIDER
    assert config.model == "gpt-custom"


def test_provider_override_drops_model_only_user_setting(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    settings_dir = home / ".wisp"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.json").write_text('{"model": "gpt-custom"}', encoding="utf-8")
    monkeypatch.setenv("WISP_PROVIDER", "anthropic")
    monkeypatch.delenv("WISP_MODEL", raising=False)

    config = WispConfig.from_env(session_dir=tmp_path)

    assert config.provider == "anthropic"
    assert config.model is None


def test_provider_env_override_drops_persisted_model_and_effort(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    persist_user_model_selection("openai", "gpt-5.6-sol", "high", home_dir=home)
    monkeypatch.setenv("WISP_PROVIDER", "anthropic")
    monkeypatch.delenv("WISP_MODEL", raising=False)
    monkeypatch.delenv("WISP_EFFORT", raising=False)

    config = WispConfig.from_env(session_dir=tmp_path)

    assert config.provider == "anthropic"
    assert config.model is None
    assert config.effort is None


def test_trusted_project_provider_override_drops_persisted_model_and_effort(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    persist_user_model_selection("openai", "gpt-5.6-sol", "high", home_dir=home)
    settings_dir = project / ".wisp"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.json").write_text('{"provider": "anthropic"}', encoding="utf-8")

    config = WispConfig.from_env(session_dir=tmp_path, project_dir=project, trusted=True)

    assert config.provider == "anthropic"
    assert config.model is None
    assert config.effort is None


def test_env_provider_can_restore_selection_hidden_by_project_provider(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    persist_user_model_selection("openai", "gpt-5.6-sol", "high", home_dir=home)
    settings_dir = project / ".wisp"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.json").write_text('{"provider": "anthropic"}', encoding="utf-8")
    monkeypatch.setenv("WISP_PROVIDER", "openai")
    monkeypatch.delenv("WISP_MODEL", raising=False)
    monkeypatch.delenv("WISP_EFFORT", raising=False)

    config = WispConfig.from_env(session_dir=tmp_path, project_dir=project, trusted=True)

    assert config.provider == "openai"
    assert config.model == "gpt-5.6-sol"
    assert config.effort == "high"


def test_config_effort_from_user_settings_file(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("WISP_EFFORT", raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(tmp_path)
    wisp_dir = home / ".wisp"
    wisp_dir.mkdir(parents=True)
    (wisp_dir / "settings.json").write_text('{"effort": "xhigh"}', encoding="utf-8")

    config = WispConfig.from_env(session_dir=tmp_path)

    assert config.effort == "xhigh"


def test_config_project_settings_cannot_set_effort(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # Security: effort is user-only even for a trusted project, matching
    # retry_policy -- a project must not be able to force an expensive
    # reasoning tier on every prompt just by being trusted.
    monkeypatch.delenv("WISP_EFFORT", raising=False)
    monkeypatch.chdir(tmp_path)
    wisp_dir = tmp_path / ".wisp"
    wisp_dir.mkdir(parents=True)
    (wisp_dir / "settings.json").write_text('{"effort": "xhigh"}', encoding="utf-8")

    config = WispConfig.from_env(session_dir=tmp_path, trusted=True)

    assert config.effort is None


def test_config_resolves_context_reserve_from_env_and_explicit_override(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("WISP_CONTEXT_RESERVE_TOKENS", "8192")

    assert WispConfig.from_env(session_dir=tmp_path).context_reserve_tokens == 8192
    assert (
        WispConfig.from_env(
            session_dir=tmp_path,
            context_reserve_tokens=4096,
        ).context_reserve_tokens
        == 4096
    )


def test_config_resolves_auto_compaction_from_env_and_explicit_override(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("WISP_AUTO_COMPACTION", "off")

    assert WispConfig.from_env(session_dir=tmp_path).auto_compaction_enabled is False
    assert (
        WispConfig.from_env(
            session_dir=tmp_path,
            auto_compaction_enabled=True,
        ).auto_compaction_enabled
        is True
    )


def test_config_rejects_invalid_auto_compaction_env(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("WISP_AUTO_COMPACTION", "sometimes")

    with pytest.raises(ValueError, match="WISP_AUTO_COMPACTION must be one of"):
        WispConfig.from_env(session_dir=tmp_path)
