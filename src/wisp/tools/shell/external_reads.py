"""Bind an explicitly granted external read to one path and system command."""

from __future__ import annotations

import shlex
import stat
from dataclasses import dataclass
from pathlib import Path

from wisp.tools.context import ToolContext
from wisp.tools.files.paths import matches_protected_pattern
from wisp.tools.result import ToolError
from wisp.trust.scope import is_broad_root, is_within_directory

_PRIVATE_DIRECTORIES = frozenset(
    {".git", ".wisp", ".claude", ".codex", ".agents", ".ssh", ".aws", ".azure", ".gnupg"}
)
_PRIVATE_FILES = frozenset(
    {".npmrc", ".pypirc", ".git-credentials", "agents.md", "claude.md", "codex.md"}
)
_SYSTEM_ALIASES = frozenset({Path("/tmp"), Path("/var")})

# Run inside Seatbelt. Verify the opened inode and use it directly, since a
# pathname checked in the parent could be replaced before ls or cat opens it.
_READ_BOUND_INPUT = """\
import os
import stat
import sys

program, path, device, inode = sys.argv[1:]
listing = program == "/bin/ls"
flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
if listing:
    flags |= os.O_DIRECTORY
try:
    fd = os.open(path, flags)
    info = os.fstat(fd)
    valid_type = (
        stat.S_ISDIR(info.st_mode) if listing
        else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    )
    if not valid_type or (info.st_dev, info.st_ino) != (int(device), int(inode)):
        raise OSError("the input changed")
    if listing:
        os.fchdir(fd)
        os.execv("/bin/ls", ["/bin/ls", "."])
    os.dup2(fd, 0)
    os.execv("/bin/cat", ["/bin/cat"])
except OSError as exc:
    sys.exit(f"Could not read the approved external input: {exc}")
"""


def is_private_command_path(path: Path, resolved: Path) -> bool:
    """Check both names of a command path for credential and control files."""

    return (
        any(part.casefold() in _PRIVATE_DIRECTORIES for part in (*path.parts, *resolved.parts))
        or path.name.casefold() in _PRIVATE_FILES
        or resolved.name.casefold() in _PRIVATE_FILES
        or path.name.casefold().startswith(".env")
        or resolved.name.casefold().startswith(".env")
    )


@dataclass(frozen=True, slots=True)
class ExternalCommandRead:
    """The single external input authorized for a sandboxed call."""

    command: str
    executable: str
    directory: Path
    operand: Path
    target: Path
    identity: tuple[int, int]

    def validate(self) -> None:
        """Reject a moved or replaced operand before starting the process."""

        target = _resolve_within_filesystem(self.operand)
        try:
            info = target.lstat()
        except OSError as exc:
            raise ToolError(f"External input changed before launch: {self.operand}") from exc
        if (
            target != self.target
            or (info.st_dev, info.st_ino) != self.identity
            or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)
        ):
            raise ToolError(f"External input changed before launch: {self.operand}")

    def launch_command(self, interpreter: Path) -> str:
        """Build the isolated command that opens and consumes the approved inode."""

        device, inode = self.identity
        return shlex.join(
            (
                str(interpreter),
                "-I",
                "-S",
                "-c",
                _READ_BOUND_INPUT,
                self.executable,
                str(self.target),
                str(device),
                str(inode),
            )
        )


def resolve_auto_command_read_dir(path: Path, context: ToolContext) -> Path:
    """Resolve a user's startup grant to an existing external directory.

    Args:
        path (Path): User-supplied path relative to the launch working directory or absolute.
        context (ToolContext): Launch workspace and protected paths.

    Returns:
        Path: The canonical directory to grant for this session.

    Raises:
        ToolError: The path is invalid, broad, protected, or overlaps the workspace.
    """

    try:
        operand = context.cwd / path.expanduser()
    except RuntimeError as exc:
        raise ToolError(f"Could not resolve external read directory: {path}") from exc
    target = _resolve_within_filesystem(operand)
    workspace = context.boundary()
    if (
        not target.is_dir()
        or is_broad_root(target)
        or is_within_directory(target, workspace)
        or is_within_directory(workspace, target)
    ):
        raise ToolError(
            f"Auto command read grant must be a directory outside the workspace: {path}"
        )
    if _protected_input(operand, context) or _protected_input(target, context):
        raise ToolError(f"Auto command read grant cannot include a protected directory: {path}")
    return target


def external_command_read(
    command: str, directory: Path, context: ToolContext
) -> ExternalCommandRead | None:
    """Match a single, flag-free system ls or cat against a user-granted root."""

    if not command.startswith(("/bin/ls ", "/bin/cat ")):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if len(tokens) != 2 or not Path(tokens[1]).is_absolute():
        return None
    executable, raw_operand = tokens
    operand = Path(raw_operand)
    try:
        if not _normalize_system_alias(operand).is_relative_to(directory):
            return None
        if _protected_input(operand, context):
            return None
        target = _resolve_within_filesystem(operand)
        if not target.is_relative_to(directory) or _protected_input(target, context):
            return None
        info = target.lstat()
    except (OSError, RuntimeError):
        return None
    if executable == "/bin/ls":
        if not stat.S_ISDIR(info.st_mode):
            return None
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return None
    return ExternalCommandRead(
        command, executable, directory, operand, target, (info.st_dev, info.st_ino)
    )


def _normalize_system_alias(path: Path) -> Path:
    # macOS spells these system directories under /private after resolution.
    for alias in _SYSTEM_ALIASES:
        if path.is_relative_to(alias):
            return alias.resolve() / path.relative_to(alias)
    return path


def _resolve_within_filesystem(path: Path) -> Path:
    try:
        if ".." not in path.parts and not any(
            part not in _SYSTEM_ALIASES and part.is_symlink() for part in (path, *path.parents)
        ):
            return path.resolve(strict=True)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ToolError(f"Could not resolve external input: {path}") from exc
    raise ToolError(f"External inputs must use a path without symbolic links or '..': {path}")


def _protected_input(path: Path, context: ToolContext) -> bool:
    return any(
        is_private_command_path(part, part)
        or matches_protected_pattern(part, context.protected_paths)
        for part in (path, *path.parents)
    )
