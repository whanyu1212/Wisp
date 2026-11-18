"""Conservative, edit-only automatic approval; never grants shell execution."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from wisp.tools.base import Tool
from wisp.tools.context import ToolContext
from wisp.tools.files.operations import EditTool, WriteTool
from wisp.tools.files.secure_fs import secure_tool_path
from wisp.trust.scope import is_broad_root

# These files can change future agent behavior or Git execution. They need the
# user's own approval even when ordinary source edits are automatic.
_CONTROL_DIRECTORIES = frozenset({".git", ".wisp", ".claude", ".codex", ".agents"})
_CONTROL_FILES = frozenset({"agents.md", "claude.md", "codex.md"})


def eligible_workspace_edit(
    tool: Tool, arguments: Mapping[str, object], context: ToolContext
) -> bool:
    """Check an exact built-in edit against the current workspace boundary.

    Explicitly bounds Auto even when an embedder allows outside-workspace tools.
    This is only approval preflight: the tool must repeat its race-resistant
    opens, write constraints, and stale-input checks when it actually executes.

    Args:
        tool (Tool): Resolved implementation, not a model-supplied tool name.
        arguments (Mapping[str, object]): The proposed call's arguments.
        context (ToolContext): Current workspace, scratch, and path restrictions.

    Returns:
        bool: Whether this call qualifies for edit-only Auto approval. False
            requires the ordinary approval flow, not an automatic grant.

    Raises:
        ToolError: An existing path or write restriction rejects the destination.
    """

    # Windows file updates currently use in-place writes. Keep their explicit
    # approval until atomic-only execution is supported there as well.
    if os.name != "posix" or type(tool) not in (WriteTool, EditTool):
        return False
    selected = arguments.get("path")
    if not isinstance(selected, str) or not selected:
        return False
    path = secure_tool_path(selected, context, write=True).path
    # Prefer the narrower scratch grant when a broad workspace (such as home)
    # contains it; ToolContext.boundary_for deliberately prefers the workspace.
    scratch = context.scratch_root
    boundary = (
        scratch
        if scratch is not None and path.is_relative_to(scratch)
        else context.boundary_for(path)
    )
    if boundary is None or is_broad_root(boundary):
        return False
    try:
        if path.lstat().st_nlink > 1:
            return False
    except FileNotFoundError:
        pass
    relative = path.relative_to(boundary)
    parts = tuple(part.casefold() for part in relative.parts)
    return not (
        any(part in _CONTROL_DIRECTORIES for part in parts)
        or (parts and parts[-1] in _CONTROL_FILES)
        or _inside_control_directory(boundary, scratch=boundary == context.scratch_root)
    )


def _inside_control_directory(path: Path, *, scratch: bool) -> bool:
    # Wisp's private session scratch normally lives *under* ~/.wisp. That does
    # not make it agent configuration. Never exempt .wisp within the scratch
    # directory itself, or treat a control directory itself as a scratch root.
    excluded = _CONTROL_DIRECTORIES - {".wisp"} if scratch else _CONTROL_DIRECTORIES
    return path.name.casefold() in _CONTROL_DIRECTORIES or any(
        part.casefold() in excluded for part in path.parts
    )
