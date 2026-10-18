"""Read the Git status snapshot a session's prompts share, and recover it on resume."""

from __future__ import annotations

from pathlib import Path

from .git import git_deadline, run_git

MAX_GIT_STATUS_LINES = 12


# Sessions reuse one status across runs to keep the prompt cacheable, so tell the model
# it may be out of date.
_GIT_SNAPSHOT_LABEL = "git (snapshot; run `git status` for the current state):"


def read_repository_status(cwd: Path) -> str:
    """Read the Git status section of the project context once.

    A session takes this once and passes it to `build_project_context` on later runs.
    The working tree changes as the agent edits files; putting the fresh status in
    every run's prompt would change the prompt prefix and miss the provider's
    prompt cache for the whole conversation after it.

    Args:
        cwd (Path): Working directory whose repository is summarized.

    Returns:
        str: The status section, labeled as a snapshot, or an unavailable status when
            the directory is not a work tree or Git does not answer in time.
    """

    resolved_cwd = cwd.resolve(strict=False)
    with git_deadline():
        inside_work_tree = run_git(resolved_cwd, "rev-parse", "--is-inside-work-tree")
        if inside_work_tree != "true":
            return "git: unavailable"

        branch = run_git(resolved_cwd, "branch", "--show-current")
        if not branch:
            branch = run_git(resolved_cwd, "rev-parse", "--short", "HEAD") or "unknown"

        status = run_git(resolved_cwd, "status", "--short")
    if status is None:
        return f"{_GIT_SNAPSHOT_LABEL} branch {branch}; status unavailable"
    if not status:
        return f"{_GIT_SNAPSHOT_LABEL} branch {branch}; status clean"

    status_lines = status.splitlines()
    shown = status_lines[:MAX_GIT_STATUS_LINES]
    hidden_count = max(0, len(status_lines) - len(shown))
    suffix = f"\n  ... {hidden_count} more" if hidden_count else ""
    return (
        f"{_GIT_SNAPSHOT_LABEL} branch {branch}; {len(status_lines)} changed file(s)"
        f"\n  " + "\n  ".join(shown) + suffix
    )


def recover_repository_status(project_context: str, cwd: Path) -> str | None:
    """Recover the Git status snapshot a persisted project context was built with.

    A resumed session can reuse the snapshot its earlier runs sent instead of
    reading Git again, so its first request keeps the prompt prefix the provider
    already cached. This parses the section `build_project_context` writes.

    Args:
        project_context (str): A persisted ``[WISP PROJECT CONTEXT]`` system message.
        cwd (Path): Working directory the new run describes.

    Returns:
        str | None: The status section exactly as `read_repository_status` returned
            it, or None when the context is for another directory or holds no
            complete snapshot (for example an unavailable status or truncation).

    Examples:
        >>> context = "\\n".join([
        ...     "[WISP PROJECT CONTEXT]",
        ...     "cwd: /repo",
        ...     f"{_GIT_SNAPSHOT_LABEL} branch main; status clean",
        ...     "project files: none detected",
        ... ])
        >>> recover_repository_status(context, Path("/repo"))
        'git (snapshot; run `git status` for the current state): branch main; status clean'
        >>> recover_repository_status(context, Path("/elsewhere")) is None
        True
    """
    lines = project_context.split("\n")
    if len(lines) < 3 or lines[0] != "[WISP PROJECT CONTEXT]":
        return None
    if lines[1] != f"cwd: {cwd.resolve(strict=False)}":
        return None
    start = next(
        (index for index, line in enumerate(lines) if line.startswith(_GIT_SNAPSHOT_LABEL)),
        None,
    )
    if start is None:
        return None
    end = start + 1
    # Changed-file lines (and the "... N more" line) are indented under the header.
    while end < len(lines) and lines[end].startswith("  "):
        end += 1
    # A complete context always continues with the project-files section; anything
    # else (end of text or a truncation marker) means the status may be cut short.
    if end == len(lines) or not lines[end].startswith("project files:"):
        return None
    return "\n".join(lines[start:end])
