"""Configuration loading for Wisp."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from wisp.settings import DEFAULT_PROTECTED_PATHS, ResolvedSettings, resolve_settings

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
    protected_paths: tuple[str, ...] = DEFAULT_PROTECTED_PATHS

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
        """Build config from environment, settings files, and explicit overrides.

        Precedence, highest to lowest: explicit argument > environment variable >
        project ``./.wisp/settings.json`` > user ``~/.wisp/settings.json`` >
        built-in default. Settings files only fill keys left unset by the argument
        and environment layers.
        """

        if load_env_file:
            load_project_env()

        settings = resolve_settings()

        provider_name = _first_non_empty(
            provider,
            os.environ.get("WISP_PROVIDER"),
            settings.provider,
            default=DEFAULT_PROVIDER,
        )
        assert provider_name is not None

        resolved_auth_path = auth_path or default_auth_path(settings=settings)

        return cls(
            provider=provider_name,
            model=_first_non_empty(model, os.environ.get("WISP_MODEL"), settings.model),
            session_dir=session_dir or default_session_dir(settings=settings),
            auth_path=resolved_auth_path,
            protected_paths=_resolve_protected_paths(settings, auth_path=resolved_auth_path),
        )


def load_project_env() -> None:
    """Load Wisp environment defaults from the current working directory."""

    load_dotenv(dotenv_path=Path.cwd() / ".env")


def default_auth_path(*, settings: ResolvedSettings | None = None) -> Path:
    """Return the default provider credential file path.

    Precedence: ``WISP_AUTH_FILE`` env var > settings-file ``auth_path`` > default.
    """

    if env_path := os.environ.get("WISP_AUTH_FILE"):
        return Path(env_path).expanduser()
    if settings is not None and settings.auth_path:
        return Path(settings.auth_path).expanduser()
    return _DEFAULT_AUTH_PATH.expanduser()


def default_session_dir(*, settings: ResolvedSettings | None = None) -> Path:
    """Return the default JSONL session directory.

    Sessions persist to ``~/.wisp/sessions`` by default so transcripts survive
    across runs and can be resumed. Set ``WISP_SESSION_DIR`` (or pass
    ``--session-dir``), or ``session_dir`` in a settings file, to store them
    elsewhere — including a temp path for ephemeral sessions.

    Precedence: ``WISP_SESSION_DIR`` env var > settings-file ``session_dir`` >
    default.
    """

    if env_dir := os.environ.get("WISP_SESSION_DIR"):
        return Path(env_dir).expanduser()
    if settings is not None and settings.session_dir:
        return Path(settings.session_dir).expanduser()
    return _DEFAULT_SESSION_DIR.expanduser()


def _resolve_protected_paths(
    settings: ResolvedSettings, *, auth_path: Path | None = None
) -> tuple[str, ...]:
    """Return the protected-path globs, honoring a settings-file override.

    A settings file may set ``protected_paths`` to any list — including an empty
    list to disable the guard entirely. When the key is absent (``None``), the
    built-in default list applies.

    Wisp's *active* credential file (``auth_path``) is always appended, even when
    the user disabled the general guard: it is Wisp's own secret, so a custom
    ``--auth-file`` / ``WISP_AUTH_FILE`` / settings ``auth_path`` is protected the
    same way the default ``~/.wisp/auth.json`` is — not just the hard-coded default
    pattern.
    """

    if settings.protected_paths is not None:
        base = settings.protected_paths
    else:
        base = DEFAULT_PROTECTED_PATHS

    if auth_path is None:
        return base

    # Protect the exact resolved credential file as an absolute-path pattern, so it
    # is caught wherever it lives (inside or outside cwd) regardless of its name.
    auth_pattern = auth_path.expanduser().resolve(strict=False).as_posix()
    if auth_pattern in base:
        return base
    return (*base, auth_pattern)


def _first_non_empty(*values: str | None, default: str | None = None) -> str | None:
    for value in values:
        if value:
            stripped = value.strip()
            if stripped:
                return stripped
    return default
