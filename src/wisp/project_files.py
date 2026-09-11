"""Bounded, protected project metadata shared by frontends and RPC.

The scan is advisory: returned paths never authorize a later file operation.
Filesystem dependencies are loaded only while scanning; event schemas also use
this module's display-path contract without loading the tool runtime.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from time import monotonic
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wisp.tools.context import ToolContext

MAX_PROJECT_FILES = 10_000
MAX_PROJECT_PATH_CHARS = 4_096
MAX_PROJECT_FILES_REPORT_BYTES = 1024 * 1024
# POSIX relative components, with no dot traversal, controls, invalid Unicode,
# backslashes (ambiguous to Windows clients), or bidirectional display overrides.
PROJECT_PATH_PATTERN = (
    r"^(?!(?:.*\/)?\.\.?(?:\/|$))"
    r"[^/\\\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]+"
    r"(?:/[^/\\\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]+)*(?![\s\S])"
)
_DISPLAY_PATH = re.compile(PROJECT_PATH_PATTERN)


def is_display_safe_path(path: str) -> bool:
    """Return whether a path can be transmitted and displayed without rewriting it."""

    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return len(path) <= MAX_PROJECT_PATH_CHARS and _DISPLAY_PATH.fullmatch(path) is not None


class ProjectScanCancelled(Exception):
    """The caller cancelled metadata discovery."""


class ProjectScanTimedOut(Exception):
    """Discovery exceeded its cooperative deadline; discard all pending entries."""


@dataclass(frozen=True)
class FileIndexConfig:
    """Immutable inputs governing which project entries may be displayed."""

    root: Path
    context: ToolContext = field(repr=False)
    max_entries: int = 10_000
    max_depth: int = 12
    max_examined_entries: int = 50_000
    timeout_seconds: float = 2.5


@dataclass(frozen=True)
class ProjectFile:
    """A regular file in a project snapshot."""

    path: str

    @property
    def display_path(self) -> str:
        return self.path


@dataclass(frozen=True)
class ProjectDirectory:
    """A real directory in a project snapshot (symlinks are never entries)."""

    path: str

    @property
    def display_path(self) -> str:
        return f"{self.path}/"


type ProjectEntry = ProjectFile | ProjectDirectory


@dataclass(frozen=True)
class SnapshotTruncation:
    """Why a scan may represent only a bounded prefix of the project."""

    entry_limit_reached: bool = False
    depth_limit_reached: bool = False
    enumeration_limit_reached: bool = False

    @property
    def truncated(self) -> bool:
        return (
            self.entry_limit_reached or self.depth_limit_reached or self.enumeration_limit_reached
        )


@dataclass(frozen=True)
class ProjectChildren:
    """Immutable adjacency row for one directory; ``parent == ""`` is the root."""

    parent: str
    children: tuple[str, ...]


@dataclass(frozen=True)
class ProjectSnapshot:
    """One immutable, bounded view of a project hierarchy."""

    root: Path
    entries: tuple[ProjectEntry, ...] = ()
    child_adjacency: tuple[ProjectChildren, ...] = ()
    truncation: SnapshotTruncation = SnapshotTruncation()

    @property
    def paths(self) -> tuple[str, ...]:
        """The legacy fuzzy corpus projected from typed entries."""

        return tuple(entry.display_path for entry in self.entries)

    @property
    def truncated(self) -> bool:
        return self.truncation.truncated

    def children_of(self, parent: str = "") -> tuple[str, ...]:
        """Return direct typed-path children of ``parent`` (``""`` means root)."""

        normalized = parent.rstrip("/")
        for row in self.child_adjacency:
            if row.parent == normalized:
                return row.children
        return ()


@dataclass
class _ScanBudget:
    remaining: int
    deadline: float
    cancelled: Event | None

    def check(self) -> None:
        if self.cancelled is not None and self.cancelled.is_set():
            raise ProjectScanCancelled
        if monotonic() >= self.deadline:
            raise ProjectScanTimedOut


@dataclass(frozen=True)
class _ScannedEntry:
    entry: ProjectEntry
    identity: tuple[int, int]


def _scan_directory(
    current: Path,
    *,
    root: Path,
    context: ToolContext,
    expected_identity: tuple[int, int] | None,
    budget: _ScanBudget,
) -> list[_ScannedEntry] | None:
    """Collect a complete guarded directory, or omit it on enumeration exhaustion.

    Args:
        current (Path): Lexical directory selected by the traversal.
        root (Path): Backend-owned base for relative display paths.
        context (ToolContext): Resolved protected-path policy.
        expected_identity (tuple[int, int] | None): Previously observed device/inode pair.
        budget (_ScanBudget): Shared work, deadline, and cancellation limits.

    Returns:
        list[_ScannedEntry] | None: Sorted safe entries, or None if enumeration exhausted
        its budget. Identity changes yield an empty list.
    """

    from wisp.tools.paths import is_protected_path
    from wisp.tools.search import IGNORED_DIRS
    from wisp.tools.secure_fs import open_directory, secure_tool_path

    budget.check()
    with open_directory(secure_tool_path(str(current), context)) as directory:
        opened = os.fstat(directory) if isinstance(directory, int) else directory.stat()
        if expected_identity is not None and (opened.st_dev, opened.st_ino) != expected_identity:
            return []
        entries: list[os.DirEntry[str]] = []
        with os.scandir(directory) as iterator:
            while True:
                budget.check()
                if budget.remaining <= 0:
                    return None
                try:
                    entry = next(iterator)
                except StopIteration:
                    break
                budget.remaining -= 1
                # Count rejected names too, but never retain arbitrarily long names.
                if is_display_safe_path(entry.name):
                    entries.append(entry)
        budget.check()
        entries.sort(key=lambda entry: entry.name)
        result: list[_ScannedEntry] = []
        for entry in entries:
            budget.check()
            candidate = current / entry.name
            relative = candidate.relative_to(root).as_posix()
            if not is_display_safe_path(relative):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
                is_dir = stat.S_ISDIR(info.st_mode)
                if not is_dir and not stat.S_ISREG(info.st_mode):
                    continue
                if is_dir and entry.name in IGNORED_DIRS:
                    continue
                if is_protected_path(candidate, context):
                    continue
                # A guarded descriptor never follows a replacement link. Confirm
                # its lexical name still denotes the entry before exposing metadata.
                lexical = candidate.stat(follow_symlinks=False)
                if (lexical.st_dev, lexical.st_ino) != (info.st_dev, info.st_ino):
                    continue
            except (OSError, RuntimeError):
                continue
            typed = ProjectDirectory(relative) if is_dir else ProjectFile(relative)
            result.append(_ScannedEntry(typed, (info.st_dev, info.st_ino)))
        return result


def collect_project_snapshot(
    config: FileIndexConfig, *, cancelled: Event | None = None
) -> ProjectSnapshot:
    """Collect a deterministic bounded hierarchy through guarded directories.

    Unreadable or replaced entries are omitted. An over-budget directory is
    omitted in full rather than retaining a filesystem-order-dependent prefix.
    Cancellation and timeout discard the entire scan. Checks occur between OS
    operations; a blocked syscall itself cannot be interrupted.

    Args:
        config (FileIndexConfig): Backend-resolved root, policy, and scan limits.
        cancelled (Event | None): Cooperative cancellation shared with the caller.

    Returns:
        ProjectSnapshot: Sorted relative metadata and structural truncation flags.

    Raises:
        ProjectScanCancelled: The cancellation event was set.
        ProjectScanTimedOut: The cooperative deadline expired.
    """

    from wisp.tools.result import ToolError

    # Keep the root lexical: resolving here could follow a replacement symlink.
    root = Path(os.path.abspath(config.root.expanduser()))
    budget = _ScanBudget(
        max(0, config.max_examined_entries),
        monotonic() + config.timeout_seconds,
        cancelled,
    )
    collected: list[ProjectEntry] = []
    adjacency: dict[str, list[str]] = {"": []}
    stack: list[tuple[Path, int, str, tuple[int, int] | None]] = [(root, 0, "", None)]
    entry_limit_reached = False
    depth_limit_reached = False
    enumeration_limit_reached = False
    while stack:
        budget.check()
        if len(collected) >= max(0, config.max_entries):
            entry_limit_reached = True
            break
        current, depth, parent, identity = stack.pop()
        try:
            entries = _scan_directory(
                current,
                root=root,
                context=config.context,
                expected_identity=identity,
                budget=budget,
            )
        except (OSError, RuntimeError, ValueError, ToolError):
            continue
        if entries is None:
            enumeration_limit_reached = True
            break
        directories: list[tuple[Path, int, str, tuple[int, int] | None]] = []
        for scanned in entries:
            budget.check()
            if len(collected) >= max(0, config.max_entries):
                entry_limit_reached = True
                break
            entry = scanned.entry
            collected.append(entry)
            adjacency.setdefault(parent, []).append(entry.path)
            if isinstance(entry, ProjectDirectory):
                adjacency.setdefault(entry.path, [])
                if depth + 1 < max(0, config.max_depth):
                    directories.append((root / entry.path, depth + 1, entry.path, scanned.identity))
                else:
                    depth_limit_reached = True
        stack.extend(reversed(directories))
    budget.check()
    return ProjectSnapshot(
        root=root,
        entries=tuple(sorted(collected, key=lambda entry: entry.display_path)),
        child_adjacency=tuple(
            ProjectChildren(parent=parent, children=tuple(sorted(children)))
            for parent, children in sorted(adjacency.items())
        ),
        truncation=SnapshotTruncation(
            entry_limit_reached=entry_limit_reached,
            depth_limit_reached=depth_limit_reached,
            enumeration_limit_reached=enumeration_limit_reached,
        ),
    )
