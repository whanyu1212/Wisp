"""Path helpers for tools."""

from __future__ import annotations

from pathlib import Path

from wisp.tools.context import ToolContext


def resolve_tool_path(path: str | None, context: ToolContext, *, default: str = ".") -> Path:
    """Resolve a tool path relative to the context working directory."""

    selected = path if path is not None else default
    candidate = Path(selected).expanduser()
    if not candidate.is_absolute():
        candidate = context.cwd / candidate
    return candidate.resolve(strict=False)


def display_tool_path(path: Path, context: ToolContext) -> str:
    """Return a stable, user-facing path for a tool result."""

    try:
        return str(path.resolve(strict=False).relative_to(context.cwd.resolve(strict=False)))
    except ValueError:
        return str(path)
