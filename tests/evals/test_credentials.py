"""Credentials for eval runs (`evals.tool_use.credentials`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.tool_use.credentials import (
    CredentialError,
    read_provider_keys,
    resolve_credentials,
)
from wisp.auth.storage import ApiKeyCredential, JsonAuthStore


def test_env_file_passes_only_provider_keys(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "export OPENAI_API_KEY='sk-file'\n"
        "WISP_MODEL=gpt-other\n"
        "ANTHROPIC_API_KEY=sk-other\n",
        encoding="utf-8",
    )

    assert read_provider_keys(env_file) == {"OPENAI_API_KEY": "sk-file"}
    assert read_provider_keys(tmp_path / "missing") == {}


@pytest.fixture
def auth_file(tmp_path: Path) -> Path:
    path = tmp_path / "auth.json"
    JsonAuthStore(path).set("openai-codex", ApiKeyCredential(key="stored"))
    return path


def test_sources_follow_wisp_order(tmp_path: Path, auth_file: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-file\n", encoding="utf-8")

    from_file = resolve_credentials(
        ["openai", "openai-codex", "fake"], env_file=env_file, auth_file=auth_file, environ={}
    )
    from_shell = resolve_credentials(
        ["openai"], env_file=env_file, auth_file=auth_file, environ={"OPENAI_API_KEY": "sk-shell"}
    )

    assert dict(from_file.sources) == {
        "fake": "none",
        "openai": "environment",
        "openai-codex": "stored",
    }
    assert dict(from_file.environment) == {"OPENAI_API_KEY": "sk-file"}
    assert dict(from_shell.environment) == {"OPENAI_API_KEY": "sk-shell"}


def test_missing_credential_stops(tmp_path: Path, auth_file: Path) -> None:
    with pytest.raises(CredentialError, match="no credential for openai") as raised:
        resolve_credentials(["openai"], env_file=tmp_path / "none", auth_file=auth_file, environ={})

    assert "sk-" not in str(raised.value)
