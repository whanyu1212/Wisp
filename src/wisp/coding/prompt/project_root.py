"""Find the project folder that scopes trust, settings, and instruction files."""

from __future__ import annotations

from pathlib import Path

from wisp.trust.scope import is_broad_root

from .git import git_deadline, run_git

PROJECT_FILE_CANDIDATES = (
    "pyproject.toml",
    "uv.lock",
    "requirements.txt",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "pytest.ini",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "Makefile",
    "README.md",
    ".gitignore",
)


PROJECT_CONTEXT_FILE_CANDIDATES = ("AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD")


def resolve_project_context_root(cwd: Path) -> Path:
    """Resolve the project root used for trust and instruction discovery.

    The Git work-tree root wins; otherwise the nearest folder holding a project
    file, then the nearest holding an instruction file. The search never settles
    on the home folder or a folder above it: a Git repository or marker file
    there is ignored, so the result is a folder below it, or ``cwd`` itself.

    Args:
        cwd (Path): Directory from which to search for a Git or project root.

    Returns:
        Path: The discovered root, falling back to the resolved working directory.
    """

    resolved_cwd = cwd.resolve(strict=False)
    with git_deadline():
        git_root = run_git(resolved_cwd, "rev-parse", "--show-toplevel")
    if git_root:
        resolved_git_root = Path(git_root).expanduser().resolve(strict=False)
        # A repository at the home folder (such as tracked dotfiles) would make
        # every folder below it one project, sharing its trust and permissions.
        if not is_broad_root(resolved_git_root):
            return resolved_git_root

    candidates = _folders_below_broad_root(resolved_cwd)
    for candidate in candidates:
        if any((candidate / name).exists() for name in PROJECT_FILE_CANDIDATES):
            return candidate

    for candidate in candidates:
        if any((candidate / name).exists() for name in PROJECT_CONTEXT_FILE_CANDIDATES):
            return candidate
    return resolved_cwd


def _folders_below_broad_root(cwd: Path) -> tuple[Path, ...]:
    """Return ``cwd`` and its parents, stopping before the home folder or above.

    A marker file in the home folder, such as a stray ``package.json``, must not
    turn the home folder into the project for every folder below it.

    Args:
        cwd (Path): Resolved working directory.

    Returns:
        tuple[Path, ...]: Folders to search for project markers, nearest first;
        empty when ``cwd`` is itself a broad folder.
    """

    folders: list[Path] = []
    for candidate in (cwd, *cwd.parents):
        if is_broad_root(candidate):
            break
        folders.append(candidate)
    return tuple(folders)
