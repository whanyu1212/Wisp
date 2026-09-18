"""Runtime configuration package.

The package root keeps the SDK-facing import path ``wisp.config.WispConfig`` stable.
Internal code imports from the defining module: ``runtime.py`` builds the immutable
``WispConfig`` and ``settings.py`` resolves layered user and project settings.

This root imports eagerly, so nothing below it in the import graph (``wisp.mcp``,
``wisp.retry``, ``wisp.validation``) may import from ``wisp.config``.
"""

from wisp.config.runtime import (
    DEFAULT_AUTO_COMPACTION_ENABLED,
    DEFAULT_CONTEXT_RESERVE_TOKENS,
    DEFAULT_PROVIDER,
    OPENAI_COMPATIBLE_CONFIG_ENV,
    WispConfig,
    default_auth_path,
    default_session_dir,
)

__all__ = [
    "DEFAULT_AUTO_COMPACTION_ENABLED",
    "DEFAULT_CONTEXT_RESERVE_TOKENS",
    "DEFAULT_PROVIDER",
    "OPENAI_COMPATIBLE_CONFIG_ENV",
    "WispConfig",
    "default_auth_path",
    "default_session_dir",
]
