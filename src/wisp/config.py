"""Configuration loading for Wisp."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_PROVIDER = "openai-codex"
_DEFAULT_AUTH_PATH = Path("~/.wisp/auth.json")
_DEFAULT_SESSION_DIR = Path("~/.wisp/sessions")


class WispConfig(BaseModel):
    """Runtime configuration for Wisp."""

    model_config = ConfigDict(frozen=True)

    provider: str = DEFAULT_PROVIDER
    model: str | None = None
    session_dir: Path = Field(default_factory=lambda: default_session_dir())
    auth_path: Path = Field(default_factory=lambda: default_auth_path())

    @classmethod
    def from_env(
        cls,
        *,
        provider: str | None = None,
        model: str | None = None,
        session_dir: Path | None = None,
        auth_path: Path | None = None,
        load_env_file: bool = True,
    ) -> WispConfig:
        """Build config from environment variables with explicit overrides.

        Precedence is: explicit argument > environment > default.
        """

        if load_env_file:
            load_project_env()

        provider_name = _first_non_empty(
            provider,
            os.environ.get("WISP_PROVIDER"),
            default=DEFAULT_PROVIDER,
        )
        assert provider_name is not None

        return cls(
            provider=provider_name,
            model=_first_non_empty(model, os.environ.get("WISP_MODEL")),
            session_dir=session_dir or default_session_dir(),
            auth_path=auth_path or default_auth_path(),
        )


def load_project_env() -> None:
    """Load Wisp environment defaults from the current working directory."""

    load_dotenv(dotenv_path=Path.cwd() / ".env")


def default_auth_path() -> Path:
    """Return the default provider credential file path."""

    if env_path := os.environ.get("WISP_AUTH_FILE"):
        return Path(env_path).expanduser()
    return _DEFAULT_AUTH_PATH.expanduser()


def default_session_dir() -> Path:
    """Return the default JSONL session directory.

    Sessions persist to ``~/.wisp/sessions`` by default so transcripts survive
    across runs and can be resumed. Set ``WISP_SESSION_DIR`` (or pass
    ``--session-dir``) to store them elsewhere — including a temp path for
    ephemeral sessions.
    """

    if env_dir := os.environ.get("WISP_SESSION_DIR"):
        return Path(env_dir).expanduser()
    return _DEFAULT_SESSION_DIR.expanduser()


def _first_non_empty(*values: str | None, default: str | None = None) -> str | None:
    for value in values:
        if value:
            stripped = value.strip()
            if stripped:
                return stripped
    return default
