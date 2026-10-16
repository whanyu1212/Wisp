"""Recognize project folders too broad to keep trust or YOLO for.

Trust and the YOLO permission mode are saved per project folder. A folder that
contains much more than one project, such as the home folder, would carry them
to everything below it, so Wisp never saves either for such a folder: it asks
again each session instead. This module only decides what counts as broad; the
trust and permission stores enforce the rule.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

# How messages describe a broad folder to the user.
BROAD_FOLDER_DESCRIPTION = "your home folder, a folder above it, or a temporary folder"


def is_broad_root(path: Path, *, home: Path | None = None) -> bool:
    """Return whether ``path`` is too broad to keep trust or YOLO for.

    Broad folders are the home folder, every folder above it (which includes
    the filesystem root), any filesystem or drive root, and the system's
    temporary folders. Paths are compared after resolving symlinks, ``..``, and
    trailing separators, and existing folders also by file identity, so every
    spelling of the home folder matches, including other letter cases.

    Args:
        path (Path): Project folder to check.
        home (Path | None): Home folder; defaults to the current user's.

    Returns:
        bool: True when trust and YOLO must not be saved for ``path``.

    Examples:
        >>> is_broad_root(Path("/"))
        True
        >>> is_broad_root(Path("/home/me"), home=Path("/home/me"))
        True
        >>> is_broad_root(Path("/home/me/project"), home=Path("/home/me"))
        False
    """

    resolved = _resolved(path)
    if resolved.parent == resolved:
        return True
    broad = set(_temporary_folders())
    home_folder = _home(home)
    if home_folder is not None:
        broad.add(home_folder)
        broad.update(home_folder.parents)
    if resolved in broad:
        return True
    # Case-insensitive file systems and macOS firmlinks give one folder several
    # spellings that ``resolve`` keeps apart (``/users/me``, or
    # ``/System/Volumes/Data/Users/me``), so existing folders also match by identity.
    identity = _identity(resolved)
    return identity is not None and any(identity == _identity(folder) for folder in broad)


def _home(home: Path | None) -> Path | None:
    try:
        return _resolved(home if home is not None else Path.home())
    except RuntimeError:
        # No home folder can be determined; only roots and temporary folders apply.
        return None


def _temporary_folders() -> frozenset[Path]:
    return frozenset(
        _resolved(folder)
        for folder in (Path(tempfile.gettempdir()), Path("/tmp"), Path("/var/tmp"))
    )


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    # Some file systems (FAT, some network shares) report inode 0 for every entry,
    # which would make every folder on them match; compare by path alone there.
    if info.st_ino == 0:
        return None
    return (info.st_dev, info.st_ino)


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


__all__ = ["BROAD_FOLDER_DESCRIPTION", "is_broad_root"]
