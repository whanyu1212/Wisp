"""User-owned permission defaults, isolated by canonical project directory."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: ``wisp.config`` loads this module while ``wisp.events`` may still be
    # initializing, so the runtime import graph must not point back at events.
    from wisp.events import PermissionMode


def permissions_directory() -> Path:
    """Return the user-local directory reserved for permission preferences."""

    return Path.home() / ".wisp" / "permissions"


def _project_key(project: Path) -> str:
    return str(project.expanduser().resolve(strict=False))


def _preference_path(project: Path) -> Path:
    digest = hashlib.sha256(_project_key(project).encode()).hexdigest()
    return permissions_directory() / f"{digest}.json"


def _private_directory(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISDIR(info.st_mode) and (
        os.name != "posix" or (info.st_uid == os.getuid() and info.st_mode & 0o022 == 0)
    )


def load_permission_mode(project: Path) -> PermissionMode | None:
    """Read this project's saved mode, failing closed on invalid or unsafe files.

    Args:
        project (Path): Project directory; aliases resolve to the same preference.

    Returns:
        PermissionMode | None: Explicit saved mode, or None when no usable record exists.
            Callers must use approval prompts when there is no saved mode.
    """

    path = _preference_path(project)
    try:
        if not _private_directory(path.parent) or path.is_symlink():
            return None
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        with os.fdopen(fd, encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                return None
            if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
                return None
            record = json.loads(stream.read(4097))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    if not isinstance(record, dict) or record.get("project") != _project_key(project):
        return None
    mode = record.get("mode")
    if mode == "ask":
        return "ask"
    if mode == "yolo":
        return "yolo"
    return None


def save_permission_mode(project: Path, mode: PermissionMode) -> None:
    """Atomically save a permission default in the user-owned project record.

    Separate files prevent concurrent choices in different projects from overwriting
    each other. Nothing is read from or written to repository-local settings.

    Args:
        project (Path): Project directory whose future sessions use this default.
        mode (PermissionMode): Explicit user-selected default.

    Raises:
        ValueError: The mode is unsupported.
        OSError: The preference cannot be saved securely. Callers must not claim or
            apply a persistent permission change when this write fails.
    """

    if mode not in ("ask", "yolo"):
        raise ValueError(f"Unsupported permission mode: {mode!r}")
    path = _preference_path(project)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not _private_directory(path.parent):
        raise PermissionError("Permission preferences require a user-owned directory")
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = stream.name
            json.dump({"project": _project_key(project), "mode": mode}, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
