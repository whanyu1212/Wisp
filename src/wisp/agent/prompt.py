"""Default prompt and bounded project context assembly."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from wisp.agent.messages import Message
from wisp.providers.base import ToolSpec

DEFAULT_CONTEXT_MAX_CHARS = 4_000
DEFAULT_CONTEXT_FILE_MAX_CHARS = 16_000
MAX_GIT_STATUS_LINES = 12
MAX_PROJECT_FILES = 16

DEFAULT_SYSTEM_PROMPT = """You are Wisp, a concise coding agent running in a terminal.

Operate like a careful software engineering assistant:
- Inspect relevant files before proposing or making code changes.
- Use available tools when they help, but do not assume unavailable tools exist.
- Respect tool sandboxing, permissions, and the current working directory.
- Make focused changes that fit the user's request; avoid unrelated churn.
- Run relevant checks or tests when practical, and say what you ran.
- Keep final answers concise, with changed files and verification noted when relevant."""

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
PROJECT_CONTEXT_FILE_CANDIDATES = ("AGENTS.md", "CLAUDE.md")


def build_prompt_messages(
    *,
    cwd: Path,
    tools: Sequence[ToolSpec] = (),
    max_context_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
    max_context_file_chars: int = DEFAULT_CONTEXT_FILE_MAX_CHARS,
    include_project_context: bool = True,
) -> tuple[Message, ...]:
    """Build provider-facing system messages for a Wisp turn."""

    context = (
        build_project_context(
            cwd=cwd,
            tools=tools,
            max_chars=max_context_chars,
            max_context_file_chars=max_context_file_chars,
        )
        if include_project_context
        else build_untrusted_project_context(tools=tools, max_chars=max_context_chars)
    )
    return (
        Message(role="system", content=DEFAULT_SYSTEM_PROMPT),
        Message(role="system", content=context),
    )


def build_project_context(
    *,
    cwd: Path,
    tools: Sequence[ToolSpec] = (),
    max_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
    max_context_file_chars: int = DEFAULT_CONTEXT_FILE_MAX_CHARS,
) -> str:
    """Collect a bounded, low-noise project context block."""

    resolved_cwd = cwd.resolve(strict=False)
    project_root = _project_root(resolved_cwd)
    root_section = f"project root: {project_root}" if project_root != resolved_cwd else ""
    sections = [
        "[WISP PROJECT CONTEXT]",
        f"cwd: {resolved_cwd}",
        root_section,
        _git_summary(resolved_cwd),
        _project_files_summary(project_root),
        _project_context_files_section(
            project_root=project_root,
            cwd=resolved_cwd,
            max_chars=max_context_file_chars,
        ),
        _tool_summary(tools),
    ]
    return _truncate_context("\n".join(section for section in sections if section), max_chars)


def build_untrusted_project_context(
    *,
    tools: Sequence[ToolSpec] = (),
    max_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
) -> str:
    """Build a provider-facing context block without reading project-local state."""

    sections = [
        "[WISP PROJECT CONTEXT]",
        "project context: skipped because this project is not trusted",
        _tool_summary(tools),
    ]
    return _truncate_context("\n".join(sections), max_chars)


def _project_root(cwd: Path) -> Path:
    git_root = _run_git(cwd, "rev-parse", "--show-toplevel")
    if git_root:
        return Path(git_root).expanduser().resolve(strict=False)

    for candidate in (cwd, *cwd.parents):
        if any((candidate / name).exists() for name in PROJECT_FILE_CANDIDATES):
            return candidate

    for candidate in (cwd, *cwd.parents):
        if any((candidate / name).exists() for name in PROJECT_CONTEXT_FILE_CANDIDATES):
            return candidate
    return cwd


def _git_summary(cwd: Path) -> str:
    inside_work_tree = _run_git(cwd, "rev-parse", "--is-inside-work-tree")
    if inside_work_tree != "true":
        return "git: unavailable"

    branch = _run_git(cwd, "branch", "--show-current")
    if not branch:
        branch = _run_git(cwd, "rev-parse", "--short", "HEAD") or "unknown"

    status = _run_git(cwd, "status", "--short")
    if status is None:
        return f"git: branch {branch}; status unavailable"
    if not status:
        return f"git: branch {branch}; status clean"

    status_lines = status.splitlines()
    shown = status_lines[:MAX_GIT_STATUS_LINES]
    hidden_count = max(0, len(status_lines) - len(shown))
    suffix = f"\n  ... {hidden_count} more" if hidden_count else ""
    return (
        f"git: branch {branch}; {len(status_lines)} changed file(s)"
        f"\n  " + "\n  ".join(shown) + suffix
    )


def _project_files_summary(cwd: Path) -> str:
    files = [name for name in PROJECT_FILE_CANDIDATES if (cwd / name).exists()]
    if not files:
        return "project files: none detected"

    shown = files[:MAX_PROJECT_FILES]
    suffix = f"\n  ... {len(files) - len(shown)} more" if len(files) > len(shown) else ""
    return "project files:\n  " + "\n  ".join(shown) + suffix


def _project_context_files_section(*, project_root: Path, cwd: Path, max_chars: int) -> str:
    if max_chars < 1:
        return ""

    blocks: list[str] = []
    for directory in _project_directory_chain(project_root=project_root, cwd=cwd):
        for name in PROJECT_CONTEXT_FILE_CANDIDATES:
            path = directory / name
            if not path.is_file():
                continue
            relative_path = _relative_project_path(path, project_root)
            blocks.append(f"--- {relative_path} ---\n{_read_context_file(path)}")

    if not blocks:
        return ""
    return _truncate_context("project instructions:\n" + "\n\n".join(blocks), max_chars)


def _project_directory_chain(*, project_root: Path, cwd: Path) -> tuple[Path, ...]:
    try:
        relative = cwd.relative_to(project_root)
    except ValueError:
        return (project_root,)

    directories = [project_root]
    current = project_root
    for part in relative.parts:
        current = current / part
        directories.append(current)
    return tuple(directories)


def _relative_project_path(path: Path, project_root: Path) -> str:
    try:
        relative = path.relative_to(project_root)
    except ValueError:
        return str(path)
    return relative.as_posix()


def _read_context_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").rstrip()
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


def _run_git(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ("git", "-C", str(cwd), *args),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _truncate_context(text: str, max_chars: int) -> str:
    if max_chars < 1:
        return ""
    if len(text) <= max_chars:
        return text
    marker = "[context truncated]"
    if max_chars <= len(marker):
        return marker[:max_chars]
    budget = max_chars - len(marker) - 1
    return f"{text[:budget].rstrip()}\n{marker}"
