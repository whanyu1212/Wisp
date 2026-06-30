"""Configuration loading for Wisp."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from platformdirs import user_data_dir
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_PROVIDER = "fake"


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
            load_dotenv()

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


def default_session_dir() -> Path:
    """Return the default JSONL session directory.

    The env override keeps tests and shell experiments isolated without needing
    a config file format yet.
    """

    if env_dir := os.environ.get("WISP_SESSION_DIR"):
        return Path(env_dir).expanduser()
    return Path(user_data_dir("wisp", "wisp")) / "sessions"


def _first_non_empty(*values: str | None, default: str | None = None) -> str | None:
    for value in values:
        if value:
            stripped = value.strip()
            if stripped:
                return stripped
    return default
