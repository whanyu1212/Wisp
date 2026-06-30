from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch

from wisp.config import WispConfig


def test_config_defaults_to_fake_provider(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("WISP_PROVIDER", raising=False)
    monkeypatch.delenv("WISP_MODEL", raising=False)

    config = WispConfig.from_env(session_dir=tmp_path, load_env_file=False)

    assert config.provider == "fake"
    assert config.model is None
    assert config.session_dir == tmp_path


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
