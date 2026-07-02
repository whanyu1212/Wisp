from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from pytest import MonkeyPatch

from wisp import config as config_module
from wisp.config import WispConfig, default_session_dir


def test_config_defaults_to_fake_provider(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("WISP_PROVIDER", raising=False)
    monkeypatch.delenv("WISP_MODEL", raising=False)

    config = WispConfig.from_env(session_dir=tmp_path, load_env_file=False)

    assert config.provider == "fake"
    assert config.model is None
    assert config.session_dir == tmp_path


def test_config_defaults_session_dir_to_non_precreatable_private_temp(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("WISP_SESSION_DIR", raising=False)
    monkeypatch.setattr(config_module, "_DEFAULT_TEMP_SESSION_DIR", None)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    session_dir = default_session_dir()

    assert session_dir.name == "sessions"
    assert session_dir.parent.parent == tmp_path
    if hasattr(os, "getuid"):
        assert session_dir.parent.name.startswith(f"wisp-{os.getuid()}-")
    else:
        assert session_dir.parent.name.startswith("wisp-")
    assert default_session_dir() == session_dir
    if os.name == "posix":
        assert stat.S_IMODE(session_dir.parent.stat().st_mode) == 0o700


def test_config_reads_session_dir_from_env(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    selected = tmp_path / "sessions"
    monkeypatch.setenv("WISP_SESSION_DIR", str(selected))

    config = WispConfig.from_env(load_env_file=False)

    assert config.session_dir == selected


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
        "WISP_PROVIDER=openai\nWISP_MODEL=gpt-5.5\n",
        encoding="utf-8",
    )

    config = WispConfig.from_env(session_dir=tmp_path)

    assert config.provider == "openai"
    assert config.model == "gpt-5.5"


def test_explicit_config_values_override_env(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("WISP_PROVIDER", "openai")
    monkeypatch.setenv("WISP_MODEL", "gpt-5.5")

    config = WispConfig.from_env(
        provider="fake",
        model="fake-model",
        session_dir=tmp_path,
        load_env_file=False,
    )

    assert config.provider == "fake"
    assert config.model == "fake-model"
