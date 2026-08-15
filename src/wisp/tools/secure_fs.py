"""Race-resistant filesystem access for built-in tools.

Paths are policy-checked lexically, then opened component-by-component from the
filesystem root.  No operation in this module follows a symlink.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from wisp.tools.context import ToolContext
from wisp.tools.paths import is_protected_path
from wisp.tools.result import ToolError

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_TRAVERSAL_FLAGS = (
    getattr(os, "O_PATH", os.O_RDONLY)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_SUPPORTED = (
    os.name != "nt"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
)


@dataclass(frozen=True, slots=True)
class SecureToolPath:
    """A policy-checked absolute lexical tool path."""

    path: Path
    selected: str
    display: str


@dataclass(slots=True)
class OpenParent:
    """An opened, stable parent directory and an untrusted leaf name."""

    path: SecureToolPath
    fd: int
    leaf: str


def secure_tool_path(
    selected: str | None,
    context: ToolContext,
    *,
    default: str = ".",
    write: bool = False,
) -> SecureToolPath:
    """Validate a path without dereferencing any user-selected component."""

    raw = selected if selected is not None else default
    cwd = context.cwd.resolve(strict=False)
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    # abspath/normpath are lexical; unlike Path.resolve they do not dereference links.
    absolute = Path(os.path.abspath(os.path.normpath(candidate)))
    if not context.allow_outside_cwd:
        try:
            absolute.relative_to(cwd)
        except ValueError as exc:
            raise ToolError(
                f"Path is outside the tool working directory: {raw}",
                failure_code="path_outside_workspace",
                recovery_hint="Use a path inside the session working directory.",
            ) from exc
    if is_protected_path(absolute, context):
        raise ToolError(f"Access to protected path denied: {raw}")
    if write and context.allowed_write_paths is not None:
        allowed = {
            Path(os.path.abspath(os.path.normpath(p if p.is_absolute() else cwd / p)))
            for p in context.allowed_write_paths
        }
        if absolute not in allowed:
            raise ToolError(f"Write path is not allowed for this operation: {raw}")
    try:
        display = str(absolute.relative_to(cwd))
    except ValueError:
        display = str(absolute)
    return SecureToolPath(absolute, raw, display)


def _require_supported() -> None:
    if not _SUPPORTED:
        raise ToolError(
            "Secure filesystem traversal is unavailable on this platform; access denied"
        )


def _open_windows_directory_guards(directory: Path) -> list[int]:
    from wisp.skills.filesystem import open_windows_skill_directory_guard

    guards: list[int] = []
    current = Path(directory.anchor)
    try:
        guards.append(open_windows_skill_directory_guard(current))
        for part in _parts(directory):
            current /= part
            guards.append(open_windows_skill_directory_guard(current))
    except BaseException:
        _close_windows_guards(guards)
        raise
    return guards


def _close_windows_guards(guards: list[int]) -> None:
    from wisp.skills.filesystem import close_windows_handle

    for guard in reversed(guards):
        close_windows_handle(guard)


@contextmanager
def open_windows_parent(path: SecureToolPath, *, create: bool = False) -> Iterator[Path]:
    """Hold verified Windows handles for every target ancestor."""

    if os.name != "nt":
        raise ToolError("Windows guarded parent access is unavailable on this platform")
    from wisp.skills.filesystem import open_windows_skill_directory_guard

    parts = _parts(path.path)
    if not parts:
        raise ToolError(f"Path has no file name: {path.selected}")
    current = Path(path.path.anchor)
    guards: list[int] = []
    try:
        guards.append(open_windows_skill_directory_guard(current))
        for part in parts[:-1]:
            current /= part
            if create:
                try:
                    current.mkdir()
                except FileExistsError:
                    pass
            guards.append(open_windows_skill_directory_guard(current))
        yield current
    except OSError as exc:
        raise ToolError(f"Could not securely open parent for {path.display}: {exc}") from exc
    finally:
        _close_windows_guards(guards)


def _parts(path: Path) -> tuple[str, ...]:
    anchor = Path(path.anchor)
    try:
        return path.relative_to(anchor).parts
    except ValueError as exc:  # pragma: no cover - defensive for unusual Path implementations
        raise ToolError(f"Could not anchor filesystem path: {path}") from exc


def _open_root(path: Path, *, readable: bool = False) -> int:
    _require_supported()
    try:
        return os.open(path.anchor, _DIRECTORY_FLAGS if readable else _TRAVERSAL_FLAGS)
    except OSError as exc:
        raise ToolError(f"Could not open filesystem root for {path}: {exc}") from exc


def _open_child_directory(
    parent_fd: int,
    name: str,
    *,
    display: str,
    readable: bool = False,
) -> int:
    try:
        return os.open(
            name,
            _DIRECTORY_FLAGS if readable else _TRAVERSAL_FLAGS,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ToolError(f"Path contains a symbolic link or non-directory: {display}") from exc
        raise ToolError(f"Could not open directory {display}: {exc}") from exc


@contextmanager
def open_parent(path: SecureToolPath, *, create: bool = False) -> Iterator[OpenParent]:
    """Open and hold every ancestor of ``path`` without following links."""

    parts = _parts(path.path)
    if not parts:
        raise ToolError(f"Path has no file name: {path.selected}")
    current = _open_root(path.path)
    walked = Path(path.path.anchor)
    try:
        for part in parts[:-1]:
            walked /= part
            if create:
                try:
                    os.mkdir(part, 0o777, dir_fd=current)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise ToolError(f"Could not create directory {walked}: {exc}") from exc
            child = _open_child_directory(current, part, display=str(walked))
            os.close(current)
            current = child
        yield OpenParent(path, current, parts[-1])
    finally:
        os.close(current)


@contextmanager
def open_file(path: SecureToolPath) -> Iterator[int]:
    """Open a regular file through a stable parent descriptor."""

    if os.name == "nt":
        from wisp.skills.filesystem import open_path_file, resolved_open_file

        try:
            guards = _open_windows_directory_guards(path.path.parent)
        except OSError as exc:
            raise ToolError(f"Could not securely open file {path.display}: {exc}") from exc
        descriptor = -1
        try:
            descriptor = open_path_file(path.path)
            if resolved_open_file(descriptor, path=path.path) != path.path.resolve(strict=False):
                raise ToolError(f"File changed while opening: {path.selected}")
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ToolError(f"Not a regular file: {path.display}")
            yield descriptor
        except OSError as exc:
            raise ToolError(f"Could not securely open file {path.display}: {exc}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            _close_windows_guards(guards)
        return

    with open_parent(path) as parent:
        try:
            descriptor = os.open(parent.leaf, _FILE_FLAGS, dir_fd=parent.fd)
        except FileNotFoundError as exc:
            raise ToolError(
                f"File does not exist: {path.display}",
                failure_code="not_found",
                retryable=True,
                recovery_hint="Check the path with find or ls, then retry.",
            ) from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ToolError(f"Symbolic links are not allowed: {path.selected}") from exc
            raise ToolError(f"Could not open file {path.display}: {exc}") from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ToolError(f"Not a regular file: {path.display}")
            yield descriptor
        finally:
            os.close(descriptor)


@contextmanager
def open_directory(path: SecureToolPath) -> Iterator[int | Path]:
    """Open a directory without following any selected component."""

    if os.name == "nt":
        try:
            guards = _open_windows_directory_guards(path.path)
        except OSError as exc:
            raise ToolError(f"Could not securely open directory {path.display}: {exc}") from exc
        try:
            yield path.path
        finally:
            _close_windows_guards(guards)
        return

    parts = _parts(path.path)
    current = _open_root(path.path, readable=not parts)
    walked = Path(path.path.anchor)
    try:
        for index, part in enumerate(parts):
            walked /= part
            child = _open_child_directory(
                current,
                part,
                display=str(walked),
                readable=index == len(parts) - 1,
            )
            os.close(current)
            current = child
        yield current
    finally:
        os.close(current)


def stat_leaf(parent: OpenParent) -> os.stat_result | None:
    """Stat a leaf without following it, returning None when absent."""

    try:
        return os.stat(parent.leaf, dir_fd=parent.fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ToolError(f"Could not inspect path {parent.path.display}: {exc}") from exc


def file_version(info: os.stat_result) -> tuple[int, int, int, int, int]:
    """Return fields used to reject stale atomic publications."""

    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


__all__ = [
    "OpenParent",
    "SecureToolPath",
    "file_version",
    "open_directory",
    "open_file",
    "open_parent",
    "open_windows_parent",
    "secure_tool_path",
    "stat_leaf",
]
