"""Tests for the layered settings resolver."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError
from pytest import CaptureFixture, MonkeyPatch

from wisp.config import WispConfig
from wisp.settings import (
    DEFAULT_PROTECTED_PATHS,
    WispSettings,
    persist_user_effort,
    persist_user_model_selection,
    resolve_settings,
    user_settings_path,
)


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
    assert settings.retry is None


def test_project_provider_override_retains_user_selection_provenance(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "proj"
    _write_settings(home, provider="user-provider", model="user-model", effort="high")
    _write_settings(project, provider="project-provider")

    settings = resolve_settings(project_dir=project, home_dir=home, trust_project=True)

    assert settings.provider == "project-provider"
    assert settings.model == "user-model"
    assert settings.effort == "high"
    assert settings.user_provider == "user-provider"
    assert settings.model_from_user is True


def test_same_project_provider_keeps_user_model_and_effort(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "proj"
    _write_settings(home, provider="shared-provider", model="user-model", effort="high")
    _write_settings(project, provider="shared-provider")

    settings = resolve_settings(project_dir=project, home_dir=home, trust_project=True)

    assert settings.provider == "shared-provider"
    assert settings.model == "user-model"
    assert settings.effort == "high"


def test_user_settings_used_when_no_project_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_settings(home, provider="user-provider", session_dir="/tmp/user-sessions")

    settings = resolve_settings(project_dir=tmp_path / "proj", home_dir=home)

    assert settings.provider == "user-provider"
    assert settings.session_dir == "/tmp/user-sessions"


def test_untrusted_project_settings_file_is_skipped(tmp_path: Path) -> None:
    # Security: with trust_project=False the project ./.wisp/settings.json contributes
    # nothing — only the user layer is read. A cloned repo cannot inject provider/
    # model/session_dir/auth_path or redirect the credential file.
    home = tmp_path / "home"
    project = tmp_path / "proj"
    _write_settings(home, provider="user-provider")
    _write_settings(project, provider="project-provider", auth_path="/tmp/evil-auth.json")

    settings = resolve_settings(project_dir=project, home_dir=home, trust_project=False)

    assert settings.provider == "user-provider"  # project value ignored
    assert settings.auth_path is None  # project value ignored; user unset


def test_malformed_settings_file_is_ignored(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    project = tmp_path / "proj"
    wisp_dir = project / ".wisp"
    wisp_dir.mkdir(parents=True)
    (wisp_dir / "settings.json").write_text("{not valid json", encoding="utf-8")

    settings = resolve_settings(project_dir=project, home_dir=tmp_path / "home", trust_project=True)

    assert settings.provider is None  # falls through, no crash
    assert "malformed settings file" in capsys.readouterr().err


def test_non_object_settings_file_is_ignored(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    project = tmp_path / "proj"
    wisp_dir = project / ".wisp"
    wisp_dir.mkdir(parents=True)
    (wisp_dir / "settings.json").write_text("[1, 2, 3]", encoding="utf-8")

    settings = resolve_settings(project_dir=project, home_dir=tmp_path / "home", trust_project=True)

    assert settings.provider is None
    assert "expected a JSON object" in capsys.readouterr().err


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _write_settings(project, provider="p", future_key="whatever")

    settings = resolve_settings(project_dir=project, home_dir=tmp_path / "home", trust_project=True)

    assert settings.provider == "p"


def test_user_protected_paths_empty_list_is_preserved(tmp_path: Path) -> None:
    # An explicit empty list in the USER settings means "protect nothing" and must
    # not fall through to the built-in default.
    home = tmp_path / "home"
    _write_settings(home, protected_paths=[])

    settings = resolve_settings(project_dir=tmp_path / "proj", home_dir=home)

    assert settings.protected_paths == ()


def test_user_protected_paths_are_used(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_settings(home, protected_paths=["secret.txt"])

    settings = resolve_settings(project_dir=tmp_path / "proj", home_dir=home)

    assert settings.protected_paths == ("secret.txt",)


def test_project_protected_paths_are_ignored(tmp_path: Path) -> None:
    # Security: a project-controlled settings file must NOT be able to set or
    # weaken protected_paths (it could otherwise disable the secret guard). Only
    # the user layer is honored for this key.
    home = tmp_path / "home"
    project = tmp_path / "proj"
    _write_settings(home, protected_paths=["from-user.txt"])
    _write_settings(project, protected_paths=[])  # attempt to disable — ignored

    # Even a trusted project cannot set/weaken protected_paths (user-only policy).
    settings = resolve_settings(project_dir=project, home_dir=home, trust_project=True)

    assert settings.protected_paths == ("from-user.txt",)


def test_project_cannot_introduce_protected_paths(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "proj"
    _write_settings(project, protected_paths=["project-only.txt"])

    # Even a trusted project cannot introduce protected_paths (user-only policy).
    settings = resolve_settings(project_dir=project, home_dir=home, trust_project=True)

    assert settings.protected_paths is None  # project value ignored; user unset


def test_retry_settings_are_user_only_even_for_trusted_projects(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    _write_settings(home, retry={"max_retries": 3})
    _write_settings(project, retry={"max_retries": 10, "max_delay_seconds": 300})

    settings = resolve_settings(project_dir=project, home_dir=home, trust_project=True)

    assert settings.retry is not None
    assert settings.retry.max_retries == 3
    assert settings.retry.max_delay_seconds is None


def test_effort_settings_are_user_only_even_for_trusted_projects(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    _write_settings(home, effort="high")
    _write_settings(project, effort="xhigh")

    settings = resolve_settings(project_dir=project, home_dir=home, trust_project=True)

    assert settings.effort == "high"


def test_context_reserve_is_user_only_even_for_trusted_projects(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    _write_settings(home, context_reserve_tokens=8192)
    _write_settings(project, context_reserve_tokens=1)

    settings = resolve_settings(project_dir=project, home_dir=home, trust_project=True)

    assert settings.context_reserve_tokens == 8192


def test_auto_compaction_is_user_only_even_for_trusted_projects(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    _write_settings(home, auto_compaction_enabled=False)
    _write_settings(project, auto_compaction_enabled=True)

    settings = resolve_settings(project_dir=project, home_dir=home, trust_project=True)

    assert settings.auto_compaction_enabled is False


def test_update_check_is_user_only_even_for_trusted_projects(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    _write_settings(home, update_check_enabled=False)
    _write_settings(project, update_check_enabled=True)

    settings = resolve_settings(project_dir=project, home_dir=home, trust_project=True)

    assert settings.update_check_enabled is False


def test_project_cannot_introduce_update_check_setting(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    _write_settings(project, update_check_enabled=False)

    settings = resolve_settings(project_dir=project, home_dir=home, trust_project=True)

    assert settings.update_check_enabled is None


def test_project_cannot_introduce_effort(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    _write_settings(project, effort="xhigh")

    # Even a trusted project cannot introduce effort (user-only policy).
    settings = resolve_settings(project_dir=project, home_dir=home, trust_project=True)

    assert settings.effort is None  # project value ignored; user unset


def test_invalid_project_user_only_fields_do_not_discard_project_settings(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    _write_settings(
        home,
        protected_paths=["from-user.txt"],
        retry={"max_retries": 3},
    )
    _write_settings(
        project,
        provider="project-provider",
        model="project-model",
        protected_paths=42,
        retry={"max_retries": 20},
    )

    settings = resolve_settings(project_dir=project, home_dir=home, trust_project=True)

    assert settings.provider == "project-provider"
    assert settings.model == "project-model"
    assert settings.protected_paths == ("from-user.txt",)
    assert settings.retry is not None
    assert settings.retry.max_retries == 3
    assert capsys.readouterr().err == ""


def test_openai_compatible_is_user_only_even_for_trusted_projects(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    _write_settings(
        home,
        openai_compatible={
            "base_url": "https://openrouter.ai/api/v1",
            "default_model": "openai/gpt-5",
        },
    )
    _write_settings(
        project,
        openai_compatible={
            "base_url": "https://attacker.example/v1",
            "default_model": "stolen",
        },
    )

    settings = resolve_settings(project_dir=project, home_dir=home, trust_project=True)

    assert settings.openai_compatible is not None
    assert settings.openai_compatible.base_url == "https://openrouter.ai/api/v1"
    assert settings.openai_compatible.default_model == "openai/gpt-5"


def test_openai_compatible_endpoint_validation(tmp_path: Path) -> None:
    ca_bundle = tmp_path / "private-ca.pem"
    ca_bundle.write_text("test CA", encoding="utf-8")
    settings = WispSettings.model_validate(
        {
            "openai_compatible": {
                "provider_name": " local-openai ",
                "base_url": "http://localhost:11434/v1/",
                "default_model": " local-model ",
                "requires_api_key": False,
                "ca_bundle": str(ca_bundle),
            }
        }
    )

    assert settings.openai_compatible is not None
    assert settings.openai_compatible.provider_name == "local-openai"
    assert settings.openai_compatible.base_url == "http://localhost:11434/v1"
    assert settings.openai_compatible.default_model == "local-model"
    assert settings.openai_compatible.requires_api_key is False
    assert settings.openai_compatible.ca_bundle == ca_bundle.resolve()

    with pytest.raises(ValidationError, match="loopback"):
        WispSettings.model_validate(
            {
                "openai_compatible": {
                    "base_url": "http://remote.example/v1",
                    "default_model": "model",
                }
            }
        )
    with pytest.raises(ValidationError, match="absolute path"):
        WispSettings.model_validate(
            {
                "openai_compatible": {
                    "base_url": "https://example.test/v1",
                    "default_model": "model",
                    "ca_bundle": "relative-ca.pem",
                }
            }
        )
    with pytest.raises(ValidationError, match="existing file"):
        WispSettings.model_validate(
            {
                "openai_compatible": {
                    "base_url": "https://example.test/v1",
                    "default_model": "model",
                    "ca_bundle": str(tmp_path / "missing-ca.pem"),
                }
            }
        )
    for reserved_name in ("openai", "gemini"):
        with pytest.raises(
            ValidationError,
            match="conflicts with built-in provider or credential namespace",
        ):
            WispSettings.model_validate(
                {
                    "openai_compatible": {
                        "provider_name": reserved_name,
                        "base_url": "https://example.test/v1",
                        "default_model": "model",
                    }
                }
            )
    for invalid_name in (
        "123ai",
        "OpenRouter",
        "open_router",
        "openrouter::model",
        "openrouter--alt",
    ):
        with pytest.raises(ValidationError, match="start with a lowercase letter"):
            WispSettings.model_validate(
                {
                    "openai_compatible": {
                        "provider_name": invalid_name,
                        "base_url": "https://example.test/v1",
                        "default_model": "model",
                    }
                }
            )


def test_mcp_servers_are_user_only_even_for_trusted_projects(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    _write_settings(
        home,
        mcp_servers={"user-server": {"command": "user-command"}},
    )
    _write_settings(
        project,
        mcp_servers={"project-server": {"command": "project-command"}},
    )

    settings = resolve_settings(project_dir=project, home_dir=home, trust_project=True)

    assert settings.mcp_servers is not None
    assert [server.name for server in settings.mcp_servers] == ["user-server"]


def test_mcp_settings_json_round_trip_uses_name_keyed_servers() -> None:
    settings = WispSettings.model_validate(
        {
            "mcp_servers": {
                "github": {
                    "command": "server",
                    "tool_safety": {"read-file": "read"},
                }
            }
        }
    )

    serialized = settings.model_dump_json()
    data = json.loads(serialized)

    assert data["mcp_servers"] == {
        "github": {
            "command": "server",
            "args": [],
            "env": {},
            "env_from": [],
            "tool_safety": {"read-file": "read"},
        }
    }
    assert WispSettings.model_validate_json(serialized) == settings
    schema = WispSettings.model_json_schema()
    assert schema["properties"]["mcp_servers"]["anyOf"][0]["type"] == "object"


def test_invalid_project_mcp_settings_do_not_discard_other_project_settings(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    _write_settings(
        project,
        provider="project-provider",
        mcp_servers={"INVALID": {"command": 42}},
    )

    settings = resolve_settings(
        project_dir=project,
        home_dir=tmp_path / "home",
        trust_project=True,
    )

    assert settings.provider == "project-provider"
    assert settings.mcp_servers is None
    assert capsys.readouterr().err == ""


def test_invalid_user_mcp_warning_does_not_expose_environment_secret(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    secret = "super-secret\x00"
    _write_settings(
        home,
        mcp_servers={
            "server": {
                "command": "command",
                "env": {"TOKEN": secret},
            }
        },
    )

    settings = resolve_settings(project_dir=tmp_path / "project", home_dir=home)
    warning = capsys.readouterr().err

    assert settings.mcp_servers is None
    assert "ignoring invalid settings" in warning
    assert secret not in warning


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_user_mcp_settings_permissions_are_hardened_before_loading(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_settings(
        home,
        mcp_servers={
            "server": {
                "command": "server",
                "env": {"TOKEN": "super-secret"},
            }
        },
    )
    path = user_settings_path(home_dir=home)
    path.chmod(0o644)
    path.parent.chmod(0o755)

    settings = resolve_settings(project_dir=tmp_path / "project", home_dir=home)

    assert settings.mcp_servers is not None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_mcp_server_count_error_hides_nested_environment_values() -> None:
    secret = "super-secret"
    servers = {
        f"server-{index}": {
            "command": "command",
            "env": {"TOKEN": secret},
        }
        for index in range(17)
    }

    with pytest.raises(ValidationError) as captured:
        WispSettings.model_validate({"mcp_servers": servers})

    assert secret not in captured.value.json()
    assert all(error.get("input") == "<redacted>" for error in captured.value.errors())


# --- Precedence through WispConfig.from_env (CLI > env > file > default) ---
#
# The project settings layer only applies to a TRUSTED project, so tests that need
# the project ./.wisp/settings.json to take effect pass trusted=True. from_env
# defaults to trusted=False (fail-closed), which the untrusted-ignore test asserts.


def test_from_env_argument_beats_settings_file(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WISP_PROVIDER", raising=False)
    _write_settings(tmp_path, provider="file-provider")

    config = WispConfig.from_env(provider="arg-provider", trusted=True)

    assert config.provider == "arg-provider"


def test_from_env_env_beats_settings_file(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WISP_PROVIDER", "env-provider")
    _write_settings(tmp_path, provider="file-provider")

    config = WispConfig.from_env(trusted=True)

    assert config.provider == "env-provider"


def test_from_env_settings_file_beats_default(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WISP_PROVIDER", raising=False)
    monkeypatch.delenv("WISP_MODEL", raising=False)
    _write_settings(tmp_path, provider="file-provider", model="file-model")

    config = WispConfig.from_env(trusted=True)

    assert config.provider == "file-provider"
    assert config.model == "file-model"


def test_from_env_untrusted_project_settings_are_ignored(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # Security: an untrusted project's ./.wisp/settings.json must contribute nothing.
    # trusted defaults to False, so provider/model/session_dir/auth_path all fall
    # through to env/user/default instead of the project file.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WISP_PROVIDER", raising=False)
    monkeypatch.delenv("WISP_MODEL", raising=False)
    _write_settings(tmp_path, provider="evil-provider", model="evil-model")

    config = WispConfig.from_env()  # trusted defaults to False

    assert config.provider != "evil-provider"
    assert config.model != "evil-model"


def test_from_env_settings_file_sets_session_dir(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WISP_SESSION_DIR", raising=False)
    sessions = tmp_path / "custom-sessions"
    _write_settings(tmp_path, session_dir=str(sessions))

    config = WispConfig.from_env(trusted=True)

    assert config.session_dir == sessions


def test_from_env_env_session_dir_beats_settings_file(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    env_sessions = tmp_path / "env-sessions"
    monkeypatch.setenv("WISP_SESSION_DIR", str(env_sessions))
    _write_settings(tmp_path, session_dir=str(tmp_path / "file-sessions"))

    config = WispConfig.from_env(trusted=True)

    assert config.session_dir == env_sessions


def test_from_env_defaults_protected_paths(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    config = WispConfig.from_env()

    # The built-in defaults are present; active Wisp secret files are always appended.
    assert set(DEFAULT_PROTECTED_PATHS).issubset(config.protected_paths)
    assert any(config.auth_path.name in pattern for pattern in config.protected_paths)


def test_from_env_user_settings_can_disable_protected_paths(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # The USER (global) settings file may disable the general guard; Wisp still
    # protects its own active credential and settings files. Point HOME at an explicit
    # temp dir so the user-settings write is isolated from the real home.
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(tmp_path)
    _write_settings(home, protected_paths=[])

    config = WispConfig.from_env()

    auth_pattern = config.auth_path.resolve().as_posix()
    settings_pattern = user_settings_path(home_dir=home).resolve().as_posix()
    assert config.protected_paths == (auth_pattern, settings_pattern)


def test_from_env_project_settings_cannot_disable_protected_paths(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # Security: even a TRUSTED project's settings file must NOT disable the guard —
    # protected_paths is a user-only policy regardless of trust.
    monkeypatch.chdir(tmp_path)
    _write_settings(tmp_path, protected_paths=[])  # project attempt — ignored

    config = WispConfig.from_env(trusted=True)

    assert set(DEFAULT_PROTECTED_PATHS).issubset(config.protected_paths)


def test_retry_policy_prefers_environment_then_user_settings(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(tmp_path)
    _write_settings(home, retry={"max_retries": 3, "base_delay_seconds": 1})
    _write_settings(tmp_path, retry={"max_retries": 10, "max_delay_seconds": 300})
    monkeypatch.setenv("WISP_RETRY_MAX_RETRIES", "1")

    config = WispConfig.from_env(trusted=True)

    assert config.retry_policy.max_retries == 1
    assert config.retry_policy.base_delay_seconds == 1
    assert config.retry_policy.max_delay_seconds == 30


# --- persisted user preferences ---


def test_persist_user_model_selection_writes_provider_model_and_effort(tmp_path: Path) -> None:
    persist_user_model_selection("anthropic", "claude-opus-4-8", "high", home_dir=tmp_path)

    path = user_settings_path(home_dir=tmp_path)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "effort": "high",
    }


def test_persist_user_model_selection_defaults_remove_model_and_effort(tmp_path: Path) -> None:
    _write_settings(
        tmp_path,
        provider="anthropic",
        model="claude-opus-4-8",
        effort="high",
        session_dir="/tmp/sessions",
    )

    persist_user_model_selection("openai", None, None, home_dir=tmp_path)

    path = user_settings_path(home_dir=tmp_path)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "provider": "openai",
        "session_dir": "/tmp/sessions",
    }


def test_persist_user_model_selection_round_trips_through_settings(tmp_path: Path) -> None:
    persist_user_model_selection("google", "gemini-3.5-flash", "HIGH", home_dir=tmp_path)

    settings = resolve_settings(project_dir=tmp_path / "proj", home_dir=tmp_path)

    assert settings.provider == "google"
    assert settings.model == "gemini-3.5-flash"
    assert settings.effort == "HIGH"


def test_persist_user_model_selection_removes_invalid_recognized_settings(
    tmp_path: Path,
) -> None:
    _write_settings(
        tmp_path,
        context_reserve_tokens=-1,
        session_dir="/tmp/sessions",
        future_setting={"enabled": True},
    )

    persist_user_model_selection("anthropic", "claude-opus-4-8", "high", home_dir=tmp_path)

    path = user_settings_path(home_dir=tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "context_reserve_tokens" not in data
    assert data["session_dir"] == "/tmp/sessions"
    assert data["future_setting"] == {"enabled": True}
    settings = resolve_settings(project_dir=tmp_path / "proj", home_dir=tmp_path)
    assert settings.provider == "anthropic"
    assert settings.model == "claude-opus-4-8"
    assert settings.effort == "high"


def test_persist_user_effort_writes_a_new_file(tmp_path: Path) -> None:
    persist_user_effort("high", home_dir=tmp_path)

    path = user_settings_path(home_dir=tmp_path)
    assert json.loads(path.read_text(encoding="utf-8")) == {"effort": "high"}


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_persist_user_settings_uses_private_permissions_and_preserves_mcp_env(
    tmp_path: Path,
) -> None:
    _write_settings(
        tmp_path,
        mcp_servers={
            "server": {
                "command": "server",
                "env": {"TOKEN": "super-secret"},
            }
        },
    )
    path = user_settings_path(home_dir=tmp_path)
    path.chmod(0o600)
    path.parent.chmod(0o755)
    previous_umask = os.umask(0o022)
    try:
        persist_user_effort("high", home_dir=tmp_path)
    finally:
        os.umask(previous_umask)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcp_servers"]["server"]["env"]["TOKEN"] == "super-secret"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_persist_user_effort_round_trips_through_resolve_settings(tmp_path: Path) -> None:
    persist_user_effort("xhigh", home_dir=tmp_path)

    settings = resolve_settings(project_dir=tmp_path / "proj", home_dir=tmp_path)

    assert settings.effort == "xhigh"


def test_persist_user_effort_preserves_other_keys(tmp_path: Path) -> None:
    _write_settings(tmp_path, provider="user-provider", model="user-model")

    persist_user_effort("medium", home_dir=tmp_path)

    path = user_settings_path(home_dir=tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["provider"] == "user-provider"
    assert data["model"] == "user-model"
    assert data["effort"] == "medium"


def test_persist_user_effort_overwrites_a_previous_value(tmp_path: Path) -> None:
    persist_user_effort("low", home_dir=tmp_path)
    persist_user_effort("high", home_dir=tmp_path)

    path = user_settings_path(home_dir=tmp_path)
    assert json.loads(path.read_text(encoding="utf-8"))["effort"] == "high"


def test_persist_user_effort_none_clears_the_key(tmp_path: Path) -> None:
    _write_settings(tmp_path, provider="user-provider", effort="high")

    persist_user_effort(None, home_dir=tmp_path)

    path = user_settings_path(home_dir=tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "effort" not in data
    assert data["provider"] == "user-provider"


def test_persist_user_effort_tolerates_a_malformed_existing_file(tmp_path: Path) -> None:
    path = user_settings_path(home_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")

    persist_user_effort("high", home_dir=tmp_path)

    assert json.loads(path.read_text(encoding="utf-8")) == {"effort": "high"}


def test_persist_user_effort_tolerates_a_non_object_existing_file(tmp_path: Path) -> None:
    path = user_settings_path(home_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")

    persist_user_effort("high", home_dir=tmp_path)

    assert json.loads(path.read_text(encoding="utf-8")) == {"effort": "high"}


def test_user_settings_path_matches_layout(tmp_path: Path) -> None:
    assert user_settings_path(home_dir=tmp_path) == tmp_path / ".wisp" / "settings.json"


def test_persist_user_effort_tolerates_an_unwritable_home_dir(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    # Regression test (Codex review on #125): persist_user_effort is called
    # from TuiShell._finish_pending_configure after a /model or /provider
    # configure has already succeeded -- a best-effort local preference write
    # failing (unwritable ~/.wisp, read-only home, full disk) must warn, not
    # raise, or it would crash the whole TUI session over a write it doesn't
    # actually need to complete.
    #
    # A plain file standing in for "home" (rather than chmod-ing a directory
    # read-only) makes path.parent.mkdir() raise NotADirectoryError
    # deterministically -- Unix permission bits don't block root or a
    # CAP_DAC_OVERRIDE-equipped process (some CI containers run as root), so
    # a chmod-based test can silently pass production code through
    # unexercised there.
    home = tmp_path / "home"
    home.write_text("not a directory", encoding="utf-8")

    persist_user_effort("high", home_dir=home)

    assert "warning" in capsys.readouterr().err.lower()


def test_persist_user_effort_does_not_overwrite_settings_it_could_not_read(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    # Regression test (Codex review on #125): a file that *exists* but can't
    # be read (permission denied, I/O error -- anything other than simply not
    # being there) must abort the write entirely rather than proceeding as if
    # the file were empty. This function's whole contract is preserving every
    # other key (provider/model/auth_path/protected_paths/retry); writing a
    # fresh {"effort": ...} over an unread file would silently destroy all of
    # them, the opposite of "best-effort." Distinct from
    # test_persist_user_effort_tolerates_a_malformed_existing_file (a
    # genuinely unreadable/corrupt document with nothing salvageable) --
    # here the file is fine, just not readable through this path right now.
    #
    # A directory-in-place-of-the-file fixture (like the write-side test
    # above) doesn't isolate this: Path.replace() also fails to overwrite a
    # directory with a file, so a *correctly* fixed function and an
    # *incorrectly* reverted one both leave the directory untouched, for
    # different reasons -- the write-side OSError handler masks the read-side
    # one. Monkeypatching Path.read_text to fail only for this exact path
    # keeps every other filesystem operation (including the real write) live,
    # so the test can assert on the actual persisted content, the real bug
    # this finding is about.
    _write_settings(tmp_path, provider="user-provider", model="user-model")
    path = user_settings_path(home_dir=tmp_path)
    real_read_text = Path.read_text

    def failing_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == path:
            raise OSError("simulated read failure")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", failing_read_text)

    persist_user_effort("high", home_dir=tmp_path)

    assert "warning" in capsys.readouterr().err.lower()
    data = json.loads(real_read_text(path, encoding="utf-8"))
    assert data == {"provider": "user-provider", "model": "user-model"}
    assert "effort" not in data
