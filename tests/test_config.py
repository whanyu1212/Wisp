from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch

from wisp import config as config_module
from wisp.config import WispConfig, default_auth_path, default_session_dir


def test_config_defaults_to_default_provider(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("WISP_PROVIDER", raising=False)
    monkeypatch.delenv("WISP_MODEL", raising=False)

    config = WispConfig.from_env(session_dir=tmp_path, load_env_file=False)

    assert config.provider == config_module.DEFAULT_PROVIDER
    assert config.model is None
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

    config = WispConfig.from_env(load_env_file=False)

    assert config.session_dir == selected


def test_config_reads_auth_path_from_env(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    selected = tmp_path / "auth.json"
    monkeypatch.setenv("WISP_AUTH_FILE", str(selected))

    config = WispConfig.from_env(load_env_file=False)

    assert config.auth_path == selected


def test_config_reads_provider_and_model_from_env(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("WISP_PROVIDER", "openai")
    monkeypatch.setenv("WISP_MODEL", "gpt-5.5")

    config = WispConfig.from_env(session_dir=tmp_path, load_env_file=False)

    assert config.provider == "openai"
    assert config.model == "gpt-5.5"


def test_config_loads_dotenv_from_working_directory(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("WISP_PROVIDER", raising=False)
    monkeypatch.delenv("WISP_MODEL", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "WISP_PROVIDER=openai\nWISP_MODEL=gpt-5.5\nWISP_AUTH_FILE=.wisp-auth.json\n",
        encoding="utf-8",
    )

    config = WispConfig.from_env(session_dir=tmp_path)

    assert config.provider == "openai"
    assert config.model == "gpt-5.5"
    assert config.auth_path == Path(".wisp-auth.json")


def test_explicit_config_values_override_env(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("WISP_PROVIDER", "openai")
    monkeypatch.setenv("WISP_MODEL", "gpt-5.5")

    config = WispConfig.from_env(
        provider="fake",
        model="fake-model",
        session_dir=tmp_path,
        auth_path=tmp_path / "auth.json",
        load_env_file=False,
    )

    assert config.provider == "fake"
    assert config.model == "fake-model"
    assert config.auth_path == tmp_path / "auth.json"
