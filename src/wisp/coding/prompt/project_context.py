"""Assemble the bounded project-context block, including trusted instruction files."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from wisp.config.settings import DEFAULT_PROTECTED_PATHS
from wisp.providers.base import ToolSpec
from wisp.tools.context import ToolContext
from wisp.tools.files.paths import is_protected_path

from .git import git_deadline
from .project_root import (
    PROJECT_CONTEXT_FILE_CANDIDATES,
    PROJECT_FILE_CANDIDATES,
    resolve_project_context_root,
)
from .repository_status import read_repository_status
from .text_budget import truncate_text

DEFAULT_CONTEXT_MAX_CHARS = 32_768


DEFAULT_CONTEXT_FILE_MAX_CHARS = 28_000


MAX_PROJECT_FILES = 16


def build_project_context(
    *,
    cwd: Path,
    tools: Sequence[ToolSpec] = (),
    max_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
    max_context_file_chars: int = DEFAULT_CONTEXT_FILE_MAX_CHARS,
    trusted_context_root: Path | None = None,
    protected_paths: tuple[str, ...] = DEFAULT_PROTECTED_PATHS,
    repository_status: str | None = None,
) -> str:
    """Collect project context within a shared Git deadline and character budget.

    Args:
        cwd (Path): Working directory from which to discover the project root.
        tools (Sequence[ToolSpec]): Tools to describe in the context block.
        max_chars (int): Maximum length of the complete block.
        max_context_file_chars (int): Maximum space available for instruction files.
        trusted_context_root (Path | None): Allowed instruction-file root; defaults
            to the discovered project root.
        protected_paths (tuple[str, ...]): Paths excluded from instruction discovery.
        repository_status (str | None): Status from `read_repository_status` to reuse
            instead of reading Git again. Reusing one snapshot keeps the prompt, and so
            the provider's cached prefix, identical across a session's runs.

    Returns:
        str: Bounded project metadata and allowed instructions, ordered from the
        project root toward the working directory. Git failures degrade to an
        unavailable status; the shared deadline is restored on exit.
    """

    with git_deadline():
        resolved_cwd = cwd.resolve(strict=False)
        project_root = resolve_project_context_root(resolved_cwd)
        resolved_trusted_context_root = (
            trusted_context_root.resolve(strict=False)
            if trusted_context_root is not None
            else project_root
        )
        root_section = f"project root: {project_root}" if project_root != resolved_cwd else ""
        sections = [
            "[WISP PROJECT CONTEXT]",
            f"cwd: {resolved_cwd}",
            root_section,
            (
                repository_status
                if repository_status is not None
                else read_repository_status(resolved_cwd)
            ),
            _project_files_summary(project_root),
            _tool_summary(tools),
        ]

        base_context = "\n".join(section for section in sections if section)
        context_file_section = _project_context_files_section(
            project_root=project_root,
            cwd=resolved_cwd,
            trusted_context_root=resolved_trusted_context_root,
            protected_paths=protected_paths,
            max_chars=min(
                max_context_file_chars, _remaining_context_budget(base_context, max_chars)
            ),
        )
        if context_file_section:
            sections.append(context_file_section)
        return _truncate_context("\n".join(section for section in sections if section), max_chars)


def build_untrusted_project_context(
    *,
    tools: Sequence[ToolSpec] = (),
    max_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
) -> str:
    """Describe available tools without reading project-local state.

    Args:
        tools (Sequence[ToolSpec]): Tools exposed to the model.
        max_chars (int): Maximum length of the returned block.

    Returns:
        str: A bounded block stating that project context was skipped.
    """

    sections = [
        "[WISP PROJECT CONTEXT]",
        "project context: skipped because this project is not trusted",
        _tool_summary(tools),
    ]
    return _truncate_context("\n".join(sections), max_chars)


def _project_files_summary(cwd: Path) -> str:
    files = [name for name in PROJECT_FILE_CANDIDATES if (cwd / name).exists()]
    if not files:
        return "project files: none detected"

    shown = files[:MAX_PROJECT_FILES]
    suffix = f"\n  ... {len(files) - len(shown)} more" if len(files) > len(shown) else ""
    return "project files:\n  " + "\n  ".join(shown) + suffix


def _project_context_files_section(
    *,
    project_root: Path,
    cwd: Path,
    trusted_context_root: Path,
    protected_paths: tuple[str, ...],
    max_chars: int,
) -> str:
    if max_chars < 1:
        return ""

    section_prefix = "project instructions:\n"
    blocks: list[str] = []
    for directory in _project_directory_chain(
        project_root=project_root,
        cwd=cwd,
        trusted_context_root=trusted_context_root,
    ):
        path = _project_context_file_from_dir(
            directory,
            trusted_context_root=trusted_context_root,
            protected_paths=protected_paths,
        )
        if path is None:
            continue
        relative_path = _relative_project_path(path, project_root)
        separator = "\n\n" if blocks else ""
        block_header = f"--- {relative_path} ---\n"
        used_chars = len(section_prefix) + len("\n\n".join(blocks))
        body_budget = max_chars - used_chars - len(separator) - len(block_header)
        if body_budget < 1:
            break
        blocks.append(f"{block_header}{_read_context_file(path, max_chars=body_budget)}")

    if not blocks:
        return ""
    return _truncate_context(section_prefix + "\n\n".join(blocks), max_chars)


def _project_context_file_from_dir(
    directory: Path,
    *,
    trusted_context_root: Path,
    protected_paths: tuple[str, ...],
) -> Path | None:
    context = ToolContext(cwd=trusted_context_root, protected_paths=protected_paths)
    for name in PROJECT_CONTEXT_FILE_CANDIDATES:
        path = directory / name
        if _is_allowed_project_context_file(path, trusted_context_root, context):
            return path
    return None


def _is_allowed_project_context_file(
    path: Path,
    trusted_context_root: Path,
    context: ToolContext,
) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        path.resolve(strict=False).relative_to(trusted_context_root)
    except ValueError:
        return False
    return not is_protected_path(path, context)


def _project_directory_chain(
    *,
    project_root: Path,
    cwd: Path,
    trusted_context_root: Path,
) -> tuple[Path, ...]:
    try:
        relative = cwd.relative_to(project_root)
    except ValueError:
        return (project_root,)

    directories = [project_root]
    current = project_root
    for part in relative.parts:
        current = current / part
        directories.append(current)
    return tuple(
        directory for directory in directories if _is_relative_to(directory, trusted_context_root)
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _relative_project_path(path: Path, project_root: Path) -> str:
    try:
        relative = path.relative_to(project_root)
    except ValueError:
        return str(path)
    return relative.as_posix()


def _read_context_file(path: Path, *, max_chars: int) -> str:
    if max_chars < 1:
        return ""
    try:
        with path.open(encoding="utf-8", errors="replace") as file:
            return _truncate_context(file.read(max_chars + 1).rstrip(), max_chars)
    except OSError as exc:
        return f"[could not read: {exc}]"


def _tool_summary(tools: Sequence[ToolSpec]) -> str:
    if not tools:
        return "allowed tools: none exposed to the model"

    lines = ["allowed tools:"]
    for tool in tools:
        description = " ".join(tool.description.split())
        if len(description) > 120:
            description = f"{description[:117].rstrip()}..."
        lines.append(f"  - {tool.name}: {description}")
    return "\n".join(lines)


def _truncate_context(text: str, max_chars: int) -> str:
    return truncate_text(text, max_chars, marker="[context truncated]")


def _remaining_context_budget(prefix: str, max_chars: int) -> int:
    if max_chars < 1:
        return 0
    separator_chars = 1 if prefix else 0
    return max(0, max_chars - len(prefix) - separator_chars)
