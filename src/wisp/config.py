"""Configuration loading for Wisp."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    ValidationError,
    field_validator,
    model_validator,
)

from wisp.mcp.config import MAX_MCP_SERVERS, McpServerConfig
from wisp.openai_compatible import OpenAICompatibleSettings
from wisp.retry import RetryPolicy
from wisp.settings import (
    DEFAULT_PROTECTED_PATHS,
    ResolvedSettings,
    resolve_settings,
    user_settings_path,
)
from wisp.validation import redact_validation_error_inputs

DEFAULT_PROVIDER = "openai-codex"
DEFAULT_CONTEXT_RESERVE_TOKENS = 16_384
DEFAULT_AUTO_COMPACTION_ENABLED = True
DEFAULT_UPDATE_CHECK_ENABLED = True
_DEFAULT_AUTH_PATH = Path("~/.wisp/auth.json")
_DEFAULT_SESSION_DIR = Path("~/.wisp/sessions")


class WispConfig(BaseModel):
    """Runtime configuration for Wisp."""

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    provider: str = DEFAULT_PROVIDER
    model: str | None = None
    effort: str | None = None
    session_dir: Path = Field(default_factory=lambda: default_session_dir())
    auth_path: Path = Field(default_factory=lambda: default_auth_path())
    protected_paths: tuple[str, ...] = DEFAULT_PROTECTED_PATHS
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    context_reserve_tokens: int = Field(default=DEFAULT_CONTEXT_RESERVE_TOKENS, ge=0)
    auto_compaction_enabled: bool = DEFAULT_AUTO_COMPACTION_ENABLED
    update_check_enabled: bool = DEFAULT_UPDATE_CHECK_ENABLED
    mcp_servers: tuple[McpServerConfig, ...] = Field(
        default=(), max_length=MAX_MCP_SERVERS, repr=False
    )
    openai_compatible: OpenAICompatibleSettings | None = None

    @model_validator(mode="wrap")
    @classmethod
    def _redact_mcp_validation_inputs(
        cls,
        value: Any,
        handler: ModelWrapValidatorHandler[Self],
    ) -> Self:
        try:
            return handler(value)
        except ValidationError as exc:
            redacted = redact_validation_error_inputs(exc, field="mcp_servers")
            if redacted is exc:
                raise
            raise redacted from None

    @field_validator("mcp_servers")
    @classmethod
    def _validate_mcp_servers(
        cls, value: tuple[McpServerConfig, ...]
    ) -> tuple[McpServerConfig, ...]:
        names = [server.name for server in value]
        if len(names) != len(set(names)):
            raise ValueError("MCP server names must be unique")
        return tuple(sorted(value, key=lambda server: server.name))

    @model_validator(mode="after")
    def _always_protect_sensitive_paths(self) -> WispConfig:
        """Ensure Wisp's credential and user settings files are always protected.

        Enforced as a model invariant — on *every* construction path, not just
        ``from_env`` — so embedding/SDK code that builds ``WispConfig`` directly
        (e.g. ``WispConfig(auth_path=Path("codex-auth.json"))``) still protects the
        sensitive files that ``ToolContext.from_config`` will honor. These files are
        protected even when ``protected_paths`` is otherwise empty.
        """

        required = (
            self.auth_path.expanduser().resolve(strict=False).as_posix(),
            user_settings_path().resolve(strict=False).as_posix(),
        )
        missing = tuple(pattern for pattern in required if pattern not in self.protected_paths)
        if missing:
            object.__setattr__(self, "protected_paths", (*self.protected_paths, *missing))
        return self

    @classmethod
    def from_env(
        cls,
        *,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        session_dir: Path | None = None,
        auth_path: Path | None = None,
        retry_policy: RetryPolicy | None = None,
        context_reserve_tokens: int | None = None,
        auto_compaction_enabled: bool | None = None,
        update_check_enabled: bool | None = None,
        mcp_servers: tuple[McpServerConfig, ...] | None = None,
        openai_compatible: OpenAICompatibleSettings | None = None,
        project_dir: Path | None = None,
        trusted: bool = False,
    ) -> WispConfig:
        """Build config from environment, settings files, and explicit overrides.

        Precedence, highest to lowest: explicit argument > environment variable >
        project ``./.wisp/settings.json`` > user ``~/.wisp/settings.json`` >
        built-in default. Settings files only fill keys left unset by the argument
        and environment layers.

        ``project_dir`` selects the directory whose project settings layer is read;
        it defaults to the current working directory. ``trusted`` is the project-trust
        decision (resolved beforehand from safe sources — the global trust store or
        the real-process ``WISP_TRUST``, never from project-controlled config). It
        gates the project ``.wisp/settings.json`` layer, which can set ``provider``,
        ``model``, ``session_dir``, or ``auth_path`` and would otherwise let an
        untrusted repo redirect Wisp's credential file or override user defaults. It
        defaults to ``False`` so a caller that forgets to pass a decision fails closed
        — an untrusted project contributes no local settings. Higher-precedence layers
        (explicit args, environment, user settings) are unaffected by trust.

        ``effort`` never consults the project settings layer, trusted or not —
        it is resolved from the USER settings file only (see
        :func:`wisp.settings.resolve_settings`), the same way ``retry_policy`` is,
        since it directly controls per-request cost/latency. Persisted user
        provider/model defaults are coupled: a higher-precedence provider override
        without a model drops the saved user model and effort rather than sending
        them to another provider.

        ``mcp_servers`` is likewise user-only because it defines commands that Wisp
        may later execute. There is deliberately no environment-variable layer for
        this structured setting.
        """

        settings = resolve_settings(project_dir=project_dir, trust_project=trusted)

        provider_override = _first_non_empty(provider, os.environ.get("WISP_PROVIDER"))
        model_override = _first_non_empty(model, os.environ.get("WISP_MODEL"))
        effort_override = _first_non_empty(effort, os.environ.get("WISP_EFFORT"))
        provider_name = provider_override or settings.provider or DEFAULT_PROVIDER
        user_defaults_provider = settings.user_provider or DEFAULT_PROVIDER
        selected_model = model_override or settings.model
        if (
            model_override is None
            and settings.model_from_user
            and provider_name != user_defaults_provider
        ):
            selected_model = None

        non_user_model_selected = model_override is not None or (
            settings.model is not None and not settings.model_from_user
        )
        user_defaults_have_provider = settings.user_provider is not None or settings.model_from_user
        provider_changes_user_defaults = (
            user_defaults_have_provider
            and provider_name != user_defaults_provider
            and not non_user_model_selected
        )
        if effort_override is not None:
            selected_effort = effort_override
        elif provider_changes_user_defaults:
            selected_effort = None
        else:
            selected_effort = settings.effort

        return cls(
            provider=provider_name,
            model=selected_model,
            effort=selected_effort,
            session_dir=session_dir or default_session_dir(settings=settings),
            auth_path=auth_path or default_auth_path(settings=settings),
            protected_paths=_resolve_protected_paths(settings),
            retry_policy=retry_policy or _resolve_retry_policy(settings),
            context_reserve_tokens=(
                context_reserve_tokens
                if context_reserve_tokens is not None
                else int(
                    _first_non_empty(
                        os.environ.get("WISP_CONTEXT_RESERVE_TOKENS"),
                        str(settings.context_reserve_tokens)
                        if settings.context_reserve_tokens is not None
                        else None,
                        default=str(DEFAULT_CONTEXT_RESERVE_TOKENS),
                    )
                    or DEFAULT_CONTEXT_RESERVE_TOKENS
                )
            ),
            auto_compaction_enabled=(
                auto_compaction_enabled
                if auto_compaction_enabled is not None
                else _resolve_bool(
                    os.environ.get("WISP_AUTO_COMPACTION"),
                    settings.auto_compaction_enabled,
                    default=DEFAULT_AUTO_COMPACTION_ENABLED,
                    name="WISP_AUTO_COMPACTION",
                )
            ),
            update_check_enabled=(
                update_check_enabled
                if update_check_enabled is not None
                else _resolve_bool(
                    os.environ.get("WISP_UPDATE_CHECK"),
                    settings.update_check_enabled,
                    default=DEFAULT_UPDATE_CHECK_ENABLED,
                    name="WISP_UPDATE_CHECK",
                )
            ),
            mcp_servers=mcp_servers if mcp_servers is not None else settings.mcp_servers or (),
            openai_compatible=(
                openai_compatible if openai_compatible is not None else settings.openai_compatible
            ),
        )


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


def _resolve_protected_paths(settings: ResolvedSettings) -> tuple[str, ...]:
    """Return the protected-path globs, honoring a settings-file override.

    A settings file may set ``protected_paths`` to any list — including an empty
    list to disable the guard entirely. When the key is absent (``None``), the
    built-in default list applies.

    Wisp's active credential and user settings files are appended separately by
    :meth:`WispConfig._always_protect_sensitive_paths`, so they stay protected
    regardless of this value (including an empty list).
    """

    if settings.protected_paths is not None:
        return settings.protected_paths
    return DEFAULT_PROTECTED_PATHS


def _first_non_empty(*values: str | None, default: str | None = None) -> str | None:
    for value in values:
        if value:
            stripped = value.strip()
            if stripped:
                return stripped
    return default


def _resolve_bool(
    env_value: str | None,
    saved_value: bool | None,
    *,
    default: bool,
    name: str,
) -> bool:
    if env_value is None:
        return saved_value if saved_value is not None else default
    normalized = env_value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be one of: 1, 0, true, false, yes, no, on, off")


def _resolve_retry_policy(settings: ResolvedSettings) -> RetryPolicy:
    """Resolve the user-owned retry policy without consulting project settings."""

    saved = settings.retry
    return RetryPolicy.model_validate(
        {
            "max_retries": _first_non_empty(
                os.environ.get("WISP_RETRY_MAX_RETRIES"),
                str(saved.max_retries) if saved and saved.max_retries is not None else None,
                default="2",
            ),
            "base_delay_seconds": _first_non_empty(
                os.environ.get("WISP_RETRY_BASE_DELAY_SECONDS"),
                str(saved.base_delay_seconds)
                if saved and saved.base_delay_seconds is not None
                else None,
                default="0.5",
            ),
            "max_delay_seconds": _first_non_empty(
                os.environ.get("WISP_RETRY_MAX_DELAY_SECONDS"),
                str(saved.max_delay_seconds)
                if saved and saved.max_delay_seconds is not None
                else None,
                default="30",
            ),
        }
    )
