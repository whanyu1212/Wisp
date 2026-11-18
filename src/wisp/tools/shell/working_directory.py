"""Resolve an explicit command directory without invoking shell composition."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from wisp.tools.context import ToolContext
from wisp.tools.files.paths import is_protected_path
from wisp.tools.result import ToolArgumentError

_CONTROL_DIRECTORIES = frozenset({".git", ".wisp", ".claude", ".codex", ".agents"})


def command_context(arguments: Mapping[str, object], context: ToolContext) -> ToolContext:
    """Resolve an optional cwd within the original workspace, retaining its boundary.

    Args:
        arguments (Mapping[str, object]): Exact Bash tool arguments.
        context (ToolContext): Original working directory and protections.

    Returns:
        ToolContext: Unchanged context or a copy with the resolved command directory.

    Raises:
        ToolArgumentError: The directory is invalid, protected, or outside the workspace.
    """
    if "cwd" not in arguments:
        return context
    value = arguments["cwd"]
    if (
        arguments.get("operation", "run") not in ("run", "start")
        or not isinstance(value, str)
        or not value.strip()
    ):
        raise ToolArgumentError("bash.cwd requires a non-empty directory for run or start")
    try:
        boundary = context.boundary()
        lexical = context.cwd.resolve() / Path(value)
        resolved = lexical.resolve(strict=True)
        if (
            not lexical.is_relative_to(boundary)
            or not resolved.is_dir()
            or not resolved.is_relative_to(boundary)
        ):
            raise ToolArgumentError("bash.cwd must be a directory inside the workspace")
        for path in (lexical, resolved):
            for ancestor in (path, *path.parents):
                if ancestor == boundary:
                    break
                if ancestor.name.casefold() in _CONTROL_DIRECTORIES or is_protected_path(
                    ancestor, context
                ):
                    raise ToolArgumentError("bash.cwd cannot enter a protected directory")
    except (OSError, ValueError, RuntimeError) as exc:
        raise ToolArgumentError("bash.cwd could not be resolved safely") from exc
    return replace(context, cwd=resolved, workspace_root=boundary)
