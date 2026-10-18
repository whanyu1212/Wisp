"""Read the Git status snapshot a session's prompts share."""

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
