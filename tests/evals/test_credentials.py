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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("OPENAI_API_KEY=sk-a # note\n", "sk-a"),
        ('OPENAI_API_KEY="sk-b" # note\n', "sk-b"),
        ("\ufeffOPENAI_API_KEY=sk-c\n", "sk-c"),
        ("export\tOPENAI_API_KEY=sk-d\n", "sk-d"),
        ("OPENAI_API_KEY=sk#e\n", "sk#e"),
        ('OPENAI_API_KEY="unclosed\n', None),
        ("OPENAI_API_KEY=\n", None),
    ],
    ids=["comment", "quoted-comment", "bom", "tab", "hash-in-value", "unclosed", "empty"],
)
def test_env_file_values(tmp_path: Path, text: str, expected: str | None) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(text, encoding="utf-8")

    assert read_provider_keys(env_file).get("OPENAI_API_KEY") == expected


def test_binary_env_file_rejected(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"OPENAI_API_KEY=\xff\xfe\n")

    with pytest.raises(CredentialError, match="not UTF-8"):
        read_provider_keys(env_file)


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
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-other\n", encoding="utf-8")

    with pytest.raises(CredentialError, match="no credential for openai: "):
        resolve_credentials(
            ["openai", "openai-codex"], env_file=env_file, auth_file=auth_file, environ={}
        )
