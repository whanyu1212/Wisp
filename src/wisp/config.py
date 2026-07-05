"""Configuration loading for Wisp."""

from __future__ import annotations

import getpass
import hashlib
import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_PROVIDER = "fake"
_DEFAULT_TEMP_SESSION_DIR: Path | None = None


class WispConfig(BaseModel):
    """Runtime configuration for Wisp."""

    model_config = ConfigDict(frozen=True)

    provider: str = DEFAULT_PROVIDER
    model: str | None = None
    session_dir: Path = Field(default_factory=lambda: default_session_dir())

    @classmethod
    def from_env(
        cls,
        *,
        provider: str | None = None,
        model: str | None = None,
        session_dir: Path | None = None,
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
        )


def load_project_env() -> None:
    """Load Wisp environment defaults from the current working directory."""

    load_dotenv(dotenv_path=Path.cwd() / ".env")


def default_session_dir() -> Path:
    """Return the default JSONL session directory.

    Sessions default to OS temp storage so early dogfooding does not leave
    durable transcripts behind unless the user opts in with WISP_SESSION_DIR or
    --session-dir.
    """

    if env_dir := os.environ.get("WISP_SESSION_DIR"):
        return Path(env_dir).expanduser()
    return _default_temp_session_dir()


def _default_temp_session_dir() -> Path:
    global _DEFAULT_TEMP_SESSION_DIR
    if _DEFAULT_TEMP_SESSION_DIR is None:
        root = Path(tempfile.mkdtemp(prefix=f"wisp-{_temp_session_owner()}-"))
        _DEFAULT_TEMP_SESSION_DIR = root / "sessions"
    return _DEFAULT_TEMP_SESSION_DIR


def _temp_session_owner() -> str:
    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        return str(getuid())
    username = getpass.getuser()
    return hashlib.sha256(username.encode("utf-8")).hexdigest()[:12]


def _first_non_empty(*values: str | None, default: str | None = None) -> str | None:
    for value in values:
        if value:
            stripped = value.strip()
            if stripped:
                return stripped
    return default
