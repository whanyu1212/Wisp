"""Configuration loading for Wisp."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from platformdirs import user_data_dir
from pydantic import BaseModel, ConfigDict, Field


class WispConfig(BaseModel):
    """Runtime configuration for the first Wisp milestone."""

    model_config = ConfigDict(frozen=True)

    provider: Literal["fake"] = "fake"
    session_dir: Path = Field(default_factory=lambda: default_session_dir())

    @classmethod
    def from_env(cls, *, session_dir: Path | None = None) -> WispConfig:
        return cls(session_dir=session_dir or default_session_dir())


def default_session_dir() -> Path:
    """Return the default JSONL session directory.

    The env override keeps tests and shell experiments isolated without needing
    a config file format yet.
    """

    if env_dir := os.environ.get("WISP_SESSION_DIR"):
        return Path(env_dir).expanduser()
    return Path(user_data_dir("wisp", "wisp")) / "sessions"
