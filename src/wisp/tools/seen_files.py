"""Versions of the files a conversation's model has read or written."""

from __future__ import annotations

import os
import threading
from pathlib import Path

from wisp.tools.result import ToolError

# Device, inode, size, modification and change times, as ``secure_fs.file_version``.
type FileVersion = tuple[int, int, int, int, int]


class SeenFiles:
    """The last version of each file the model read, wrote, or edited.

    ``write`` and ``edit`` refuse to change an existing file whose current version
    differs from the one recorded here, or that was never read, so a change made
    outside the model's view (by the user, a formatter, or another process) is never
    silently overwritten. Paths are compared after resolving symbolic links, so a
    file read through a link counts as read under its real path. Read tools run in
    parallel worker threads, so access is locked.
    """

    def __init__(self) -> None:
        self._versions: dict[Path, FileVersion] = {}
        self._lock = threading.Lock()

    def record(self, path: Path, version: FileVersion) -> None:
        """Remember ``version`` as the one the model has seen at ``path``."""

        key = _real_path(path)
        with self._lock:
            self._versions[key] = version

    def require_current(self, path: Path, version: FileVersion, *, display: str) -> None:
        """Raise unless ``version`` is the one the model last saw at ``path``.

        Args:
            path (Path): Absolute path of the existing file about to change.
            version (FileVersion): Its version now.
            display (str): The path as the model named it, for the error.

        Raises:
            ToolError: The file was never read, or changed since it was.
        """

        key = _real_path(path)
        with self._lock:
            seen = self._versions.get(key)
        if seen is None:
            raise ToolError(
                f"Read {display} before changing it",
                failure_code="stale_input",
                retryable=True,
                recovery_hint="Read the file, then retry with its current contents in mind.",
            )
        if seen != version:
            raise ToolError(
                f"{display} changed since it was last read",
                failure_code="stale_input",
                retryable=True,
                recovery_hint="Read the file again, then retry with its current contents.",
            )


def _real_path(path: Path) -> Path:
    return Path(os.path.realpath(path))
