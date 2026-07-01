"""Path helpers for tools."""

from __future__ import annotations

from pathlib import Path

from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError


def resolve_tool_path(path: str | None, context: ToolContext, *, default: str = ".") -> Path:
    """Resolve a tool path relative to the context working directory.

    By default, tools are sandboxed to ``context.cwd``. Absolute paths and
    ``~`` are accepted only when they resolve back inside that directory.
    """

    selected = path if path is not None else default
    cwd = context.cwd.resolve(strict=False)
    candidate = Path(selected).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    resolved = candidate.resolve(strict=False)
    if not context.allow_outside_cwd:
        try:
            resolved.relative_to(cwd)
        except ValueError as exc:
            raise ToolError(f"Path is outside the tool working directory: {selected}") from exc
    return resolved


def display_tool_path(path: Path, context: ToolContext) -> str:
    """Return a stable, user-facing path for a tool result."""

    try:
        return str(path.resolve(strict=False).relative_to(context.cwd.resolve(strict=False)))
    except ValueError:
        return str(path)
