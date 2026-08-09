"""Tool execution context."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from wisp.settings import DEFAULT_PROTECTED_PATHS, user_settings_path

if TYPE_CHECKING:
    from wisp.config import WispConfig


@dataclass(frozen=True)
class ToolContext:
    """Ambient state shared with tool invocations."""

    cwd: Path
    max_output_bytes: int = 50_000
    max_output_lines: int = 2_000
    allow_outside_cwd: bool = False
    # Secure by default: every construction path (including a bare
    # ``ToolContext(cwd=...)`` from embedding/SDK code) protects secrets unless a
    # caller explicitly passes ``protected_paths=()`` to opt out.
    protected_paths: tuple[str, ...] = field(default_factory=lambda: DEFAULT_PROTECTED_PATHS)

    @classmethod
    def default(cls) -> ToolContext:
        """Create a context rooted at the current working directory."""

        return cls(cwd=Path.cwd())

    @classmethod
    def from_config(cls, config: WispConfig, *, cwd: Path | None = None) -> ToolContext:
        """Create a context that honors a config's protected-path policy.

        ``cwd`` defaults to the current working directory. The protected-path
        globs come from the resolved config so file tools deny reads of secrets
        (``.env`` and friends) unless the policy is relaxed.

        The active credential and user settings files are guaranteed to be in the
        returned context's ``protected_paths``. ``WispConfig`` already enforces this
        on construction; re-asserting it here is a defensive backstop so the secrets
        stay protected even for a config produced by a path that skipped validation
        (e.g. ``model_copy``).
        """

        protected_paths = config.protected_paths
        required = (
            config.auth_path.expanduser().resolve(strict=False).as_posix(),
            user_settings_path().resolve(strict=False).as_posix(),
        )
        missing = tuple(pattern for pattern in required if pattern not in protected_paths)
        if missing:
            protected_paths = (*protected_paths, *missing)

        return cls(
            cwd=cwd if cwd is not None else Path.cwd(),
            protected_paths=protected_paths,
        )
