"""Bounded, protected project metadata shared by frontends and RPC.

The scan is advisory: returned paths never authorize a later file operation.
Filesystem dependencies are loaded only while scanning; event schemas also use
this module's display-path contract without loading the tool runtime.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from time import monotonic
from typing import TYPE_CHECKING

from typing_extensions import TypeAliasType

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
PROJECT_DIRECTORY_PATTERN = re.compile(rf"(?:^(?![\s\S])|{PROJECT_PATH_PATTERN})")
_FIRST_PROGRESS_INTERVAL_SECONDS = 0.25
_MAX_PROGRESS_REPORTS = 8


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


ProjectEntry = TypeAliasType("ProjectEntry", ProjectFile | ProjectDirectory)


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

    @classmethod
    def start(cls, config: FileIndexConfig, cancelled: Event | None) -> _ScanBudget:
        return cls(
            max(0, config.max_examined_entries), monotonic() + config.timeout_seconds, cancelled
        )

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
    reject_aliases: bool = False,
) -> list[_ScannedEntry] | None:
    """Collect a complete guarded directory, or omit it on enumeration exhaustion.

    Args:
        current (Path): Lexical directory selected by the traversal.
        root (Path): Backend-owned base for relative display paths.
        context (ToolContext): Resolved protected-path policy.
        expected_identity (tuple[int, int] | None): Previously observed device/inode pair.
        budget (_ScanBudget): Shared work, deadline, and cancellation limits.
        reject_aliases (bool): Require a Windows directory supplied by the client to use
            its canonical filesystem name.

    Returns:
        list[_ScannedEntry] | None: Sorted safe entries, or None if enumeration exhausted
        its budget. Identity changes yield an empty list.
    """

    from wisp.tools.files.paths import is_protected_path
    from wisp.tools.files.secure_fs import open_directory, secure_tool_path
    from wisp.tools.search.tools import IGNORED_DIRS

    budget.check()
    secured = secure_tool_path(str(current), context)
    prefix = "" if current == root else f"{current.relative_to(root).as_posix()}/"
    with open_directory(secured) as directory:
        guarded_descriptor = isinstance(directory, int)
        if (
            reject_aliases
            and isinstance(directory, Path)
            and directory.resolve(strict=True) != current
        ):
            # On Windows, aliases such as a DOS short name can address an ignored
            # or protected directory without spelling its real components.
            raise ValueError("Project directory uses a filesystem alias")
        opened = os.fstat(directory) if isinstance(directory, int) else directory.stat()
        opened_identity = (opened.st_dev, opened.st_ino)
        if expected_identity is not None and opened_identity != expected_identity:
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
            relative = prefix + entry.name
            if not is_display_safe_path(relative):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
                is_dir = stat.S_ISDIR(info.st_mode)
                if not is_dir and not stat.S_ISREG(info.st_mode):
                    continue
                if is_dir and entry.name.casefold() in IGNORED_DIRS:
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
        if guarded_descriptor:
            budget.check()
            # A rename followed by a symlink to the same directory can pass a
            # lexical stat against each leaf. Reopen the directory without
            # following links and confirm its identity before exposing any names.
            with open_directory(secured) as verified:
                assert isinstance(verified, int)
                reopened = os.fstat(verified)
                if (reopened.st_dev, reopened.st_ino) != opened_identity:
                    raise OSError("Project directory changed during discovery")
        return result


def collect_project_directory(
    config: FileIndexConfig, directory: str, *, cancelled: Event | None = None
) -> ProjectSnapshot:
    """Collect only the requested directory's direct children.

    Args:
        config (FileIndexConfig): Backend-resolved root, policy, and scan limits.
        directory (str): A validated relative POSIX directory, or an empty string for the root.
        cancelled (Event | None): Cooperative cancellation shared with the caller.

    Returns:
        ProjectSnapshot: Direct children with project-relative paths and truncation flags.

    Raises:
        ValueError: The directory uses a filesystem alias or is excluded by policy.
        ToolError: The directory is missing, unreadable, or goes through a symbolic link.
        OSError: Filesystem enumeration failed.
        ProjectScanCancelled: The cancellation event was set.
        ProjectScanTimedOut: The cooperative deadline expired.
    """

    from wisp.tools.files.paths import is_protected_path
    from wisp.tools.search.tools import IGNORED_DIRS

    root = Path(os.path.abspath(config.root.expanduser()))
    budget = _ScanBudget.start(config, cancelled)
    budget.check()
    if is_protected_path(root, config.context):
        raise ValueError("Project directory is excluded by policy")
    parts = directory.split("/") if directory else []
    if len(parts) > max(0, config.max_depth):
        raise ValueError("Project directory exceeds the discovery depth limit")
    current = root
    for part in parts:
        budget.check()
        component = Path(part)
        if component.parts != (part,) or component.anchor or part == "..":
            raise ValueError("Invalid project directory component")
        current /= component
        if part.casefold() in IGNORED_DIRS or is_protected_path(current, config.context):
            raise ValueError("Project directory is excluded by policy")
    if parts and len(parts) >= config.max_depth:
        return ProjectSnapshot(root=root, truncation=SnapshotTruncation(depth_limit_reached=True))

    scanned = _scan_directory(
        current,
        root=root,
        context=config.context,
        expected_identity=None,
        budget=budget,
        reject_aliases=bool(parts),
    )
    budget.check()
    if scanned is None:
        return ProjectSnapshot(
            root=root, truncation=SnapshotTruncation(enumeration_limit_reached=True)
        )
    entries = sorted((item.entry for item in scanned), key=lambda entry: entry.display_path)
    max_entries = max(0, config.max_entries)
    retained = tuple(entries[:max_entries])
    return ProjectSnapshot(
        root=root,
        entries=retained,
        child_adjacency=(
            ProjectChildren(directory, tuple(sorted(entry.path for entry in retained))),
        ),
        truncation=SnapshotTruncation(entry_limit_reached=len(entries) > max_entries),
    )


def collect_project_snapshot(
    config: FileIndexConfig,
    *,
    cancelled: Event | None = None,
    on_progress: Callable[[ProjectSnapshot], None] | None = None,
) -> ProjectSnapshot:
    """Collect a deterministic bounded hierarchy through guarded directories.

    Unreadable or replaced entries are omitted. An over-budget directory is
    omitted in full rather than retaining a filesystem-order-dependent prefix.
    Cancellation and timeout discard the remaining scan. Checks occur between OS
    operations; a blocked syscall itself cannot be interrupted.

    Args:
        config (FileIndexConfig): Backend-resolved root, policy, and scan limits.
        cancelled (Event | None): Cooperative cancellation shared with the caller.
        on_progress (Callable[[ProjectSnapshot], None] | None): Receive bounded nonempty
            snapshots between directories. Callback errors and an unreadable root stop
            a progressive scan.

    Returns:
        ProjectSnapshot: Sorted relative metadata and structural truncation flags.

    Raises:
        ProjectScanCancelled: The cancellation event was set.
        ProjectScanTimedOut: The cooperative deadline expired.
    """

    from wisp.tools.result import ToolError

    # Keep the root lexical: resolving here could follow a replacement symlink.
    root = Path(os.path.abspath(config.root.expanduser()))
    budget = _ScanBudget.start(config, cancelled)
    collected: list[ProjectEntry] = []
    adjacency: dict[str, list[str]] = {"": []}
    stack: list[tuple[Path, int, str, tuple[int, int] | None]] = [(root, 0, "", None)]
    entry_limit_reached = False
    depth_limit_reached = False
    enumeration_limit_reached = False
    progress_count = 0
    progress_interval = _FIRST_PROGRESS_INTERVAL_SECONDS
    next_progress_at = 0.0
    progress_entry_count = 0
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
            if not parent and on_progress is not None:
                # A refresh of an inaccessible root must preserve a usable catalog.
                raise
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
        if (
            on_progress is not None
            and stack
            and len(collected) > progress_entry_count
            and not entry_limit_reached
            and progress_count < _MAX_PROGRESS_REPORTS
            and monotonic() >= next_progress_at
        ):
            on_progress(
                ProjectSnapshot(
                    root=root,
                    entries=tuple(sorted(collected, key=lambda entry: entry.display_path)),
                    truncation=SnapshotTruncation(depth_limit_reached=depth_limit_reached),
                )
            )
            progress_count += 1
            progress_entry_count = len(collected)
            # Give quick scans frequent updates without spending the entire
            # report budget in the first seconds of a much larger scan.
            next_progress_at = monotonic() + progress_interval
            progress_interval *= 2
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
