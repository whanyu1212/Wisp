"""Immutable project snapshots and fuzzy matching for the inline ``@`` picker.

The filesystem walk is deliberately free of Textual imports. It produces one
bounded, typed snapshot which the UI can safely replace as a single event-loop
operation; fuzzy matching remains a projection over the same display paths users
already see.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from wisp.tools.context import ToolContext
from wisp.tools.paths import is_protected_path
from wisp.tools.search import IGNORED_DIRS

_BOUNDARY_CHARS = frozenset("/_-. ")
_JSON_DECODER = json.JSONDecoder()


@dataclass(frozen=True)
class FileIndexConfig:
    """Immutable inputs governing which project entries may be displayed."""

    root: Path
    context: ToolContext
    max_entries: int = 10_000
    max_depth: int = 12


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

    @property
    def truncated(self) -> bool:
        return self.entry_limit_reached or self.depth_limit_reached


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


@dataclass(frozen=True)
class FileIndexRequest:
    """Immutable raw scan inputs captured without filesystem work on the UI thread."""

    generation: int
    cwd: str
    protected_paths: tuple[str, ...] | None = None
    adopted_auth_paths: tuple[str, ...] = ()
    max_entries: int = 10_000
    max_depth: int = 12


@dataclass(frozen=True)
class ScoredPath:
    """One ranked candidate. ``offsets`` are match positions for highlighting."""

    path: str
    score: int
    offsets: tuple[int, ...] = field(default=())


def collect_project_snapshot(config: FileIndexConfig) -> ProjectSnapshot:
    """Iteratively scan ``config.root`` into an immutable typed hierarchy.

    Ordering is deterministic, traversal is bounded by ``max_entries`` and
    ``max_depth``, unreadable/racing paths are skipped, and every kind of symlink
    (file, directory, or dangling) is omitted. Only regular files and real
    directories become entries.
    """

    try:
        root = config.root.expanduser().resolve(strict=False)
        root_stat = root.stat(follow_symlinks=False)
    except (OSError, RuntimeError):
        return ProjectSnapshot(root=config.root)
    if not root.is_dir() or root.is_symlink():
        return ProjectSnapshot(root=root)

    collected: list[ProjectEntry] = []
    adjacency: dict[str, list[str]] = {"": []}
    root_identity = (root_stat.st_dev, root_stat.st_ino)
    stack: list[tuple[Path, int, str, tuple[int, int]]] = [(root, 0, "", root_identity)]
    entry_limit_reached = False
    depth_limit_reached = False

    while stack:
        if len(collected) >= max(0, config.max_entries):
            entry_limit_reached = True
            break
        current, depth, parent, expected_identity = stack.pop()
        try:
            current_stat = current.stat(follow_symlinks=False)
            if (
                current.is_symlink()
                or (
                    current_stat.st_dev,
                    current_stat.st_ino,
                )
                != expected_identity
            ):
                continue
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
            # A queued directory can be replaced after its parent was scanned. Do
            # not consume names unless the lexical path still identifies the same
            # real directory after materialization.
            current_stat = current.stat(follow_symlinks=False)
            if (
                current.is_symlink()
                or (
                    current_stat.st_dev,
                    current_stat.st_ino,
                )
                != expected_identity
            ):
                continue
        except OSError:
            continue

        directories_to_visit: list[tuple[Path, int, str, tuple[int, int]]] = []
        for entry in entries:
            if len(collected) >= max(0, config.max_entries):
                entry_limit_reached = True
                break
            candidate = current / entry.name
            try:
                if entry.is_symlink():
                    continue
                entry_stat = entry.stat(follow_symlinks=False)
                candidate_stat = candidate.stat(follow_symlinks=False)
                entry_identity = (entry_stat.st_dev, entry_stat.st_ino)
                if (candidate_stat.st_dev, candidate_stat.st_ino) != entry_identity:
                    # The directory or entry changed after scandir. In particular,
                    # this rejects names read through a transient replacement link.
                    continue
                canonical = candidate.resolve(strict=True)
                canonical.relative_to(root)
                relative = candidate.relative_to(root).as_posix()
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except (OSError, RuntimeError, ValueError):
                continue

            if is_dir and entry.name in IGNORED_DIRS:
                continue
            if not is_dir and not is_file:
                continue
            if is_protected_path(candidate, config.context):
                continue

            typed_entry: ProjectEntry
            if is_dir:
                typed_entry = ProjectDirectory(relative)
            else:
                typed_entry = ProjectFile(relative)
            collected.append(typed_entry)
            adjacency.setdefault(parent, []).append(relative)

            if is_dir:
                adjacency.setdefault(relative, [])
                if depth + 1 < max(0, config.max_depth):
                    directories_to_visit.append((candidate, depth + 1, relative, entry_identity))
                else:
                    depth_limit_reached = True

        # The stack is LIFO; reverse here to visit lexical-first while retaining an
        # iterative walk. Final entries and every adjacency row are sorted as well.
        stack.extend(reversed(directories_to_visit))

    sorted_entries = tuple(sorted(collected, key=lambda entry: entry.display_path))
    child_adjacency = tuple(
        ProjectChildren(parent=parent, children=tuple(sorted(children)))
        for parent, children in sorted(adjacency.items())
    )
    return ProjectSnapshot(
        root=root,
        entries=sorted_entries,
        child_adjacency=child_adjacency,
        truncation=SnapshotTruncation(
            entry_limit_reached=entry_limit_reached,
            depth_limit_reached=depth_limit_reached,
        ),
    )


def collect_paths(config: FileIndexConfig) -> tuple[str, ...]:
    """Compatibility projection of :func:`collect_project_snapshot` to paths."""

    return collect_project_snapshot(config).paths


def format_file_reference(path: str) -> str:
    """Format ``path`` as an ``@`` reference, using JSON quoting when needed."""

    needs_quoting = any(
        character.isspace() or character in {'"', "\\"} or ord(character) < 0x20
        for character in path
    )
    rendered = json.dumps(path, ensure_ascii=False) if needs_quoting else path
    return f"@{rendered}"


def parse_file_reference(
    text: str,
    *,
    start: int,
    limit: int | None = None,
) -> tuple[int, str | None] | None:
    """Parse one formatter-compatible reference at ``start``.

    The returned end offset uses Python codepoint indices. A ``None`` path means
    the complete bounded token is reference-shaped but malformed; a ``None``
    return means there is no complete reference inside the supplied bound.
    """

    scan_limit = len(text) if limit is None else min(max(0, limit), len(text))
    if start < 0 or start >= scan_limit or text[start] != "@":
        return None
    value_start = start + 1
    if value_start >= scan_limit:
        return None

    if text[value_start] != '"':
        end = value_start
        while end < scan_limit and not text[end].isspace():
            end += 1
        if end == value_start:
            return None
        if end == scan_limit and scan_limit < len(text) and not text[scan_limit].isspace():
            return None
        return end, text[value_start:end]

    encoded = text[value_start:scan_limit]
    try:
        decoded, consumed = _JSON_DECODER.raw_decode(encoded)
    except (json.JSONDecodeError, RecursionError):
        if scan_limit < len(text):
            return None
        return scan_limit, None
    if not isinstance(decoded, str):
        return None

    end = value_start + consumed
    if end == scan_limit and scan_limit < len(text) and not text[scan_limit].isspace():
        return None
    if end < scan_limit and not text[end].isspace():
        while end < scan_limit and not text[end].isspace():
            end += 1
        if end == scan_limit and scan_limit < len(text):
            return None
        return end, None
    return end, decoded


def score_path(path: str, query: str) -> ScoredPath | None:
    """Score ``path`` against ``query``; ``None`` when it doesn't match.

    Matching is a smart-case subsequence. Consecutive runs, word boundaries and
    basename matches receive bonuses, preserving the picker's existing ranking.
    """

    if not query:
        return ScoredPath(path=path, score=0, offsets=())

    case_sensitive = any(character.isupper() for character in query)
    haystack = path if case_sensitive else path.lower()
    needle = query if case_sensitive else query.lower()
    basename_start = path.rfind("/") + 1

    best: ScoredPath | None = None
    start = haystack.find(needle[0])
    while start != -1:
        offsets = _align_from(haystack, needle, start)
        if offsets is None:
            break
        candidate = _score_offsets(path, offsets, basename_start)
        if best is None or candidate.score > best.score:
            best = candidate
        start = haystack.find(needle[0], start + 1)

    return best


def _align_from(haystack: str, needle: str, start: int) -> tuple[int, ...] | None:
    offsets = [start]
    cursor = start + 1
    for character in needle[1:]:
        found = haystack.find(character, cursor)
        if found == -1:
            return None
        offsets.append(found)
        cursor = found + 1
    return tuple(offsets)


def _score_offsets(path: str, offsets: tuple[int, ...], basename_start: int) -> ScoredPath:
    score = 0
    run_length = 0
    previous_offset = -2
    for offset in offsets:
        if offset == previous_offset + 1:
            run_length += 1
            score += 8 * run_length
        else:
            run_length = 0

        preceding = path[offset - 1] if offset > 0 else "/"
        if preceding in _BOUNDARY_CHARS:
            score += 12
        elif path[offset].isupper() and preceding.islower():
            score += 8

        if offset >= basename_start:
            score += 6
        previous_offset = offset

    span = offsets[-1] - offsets[0] + 1
    score += max(0, 30 - span)
    score += max(0, 40 - len(path)) // 4
    return ScoredPath(path=path, score=score, offsets=offsets)


def filter_paths(paths: tuple[str, ...], query: str, *, limit: int = 30) -> tuple[ScoredPath, ...]:
    """Rank ``paths`` against ``query``, best first, capped at ``limit``."""

    if not query:
        return tuple(ScoredPath(path=path, score=0) for path in paths[:limit])

    scored = [result for path in paths if (result := score_path(path, query)) is not None]
    scored.sort(key=lambda result: (-result.score, len(result.path), result.path))
    return tuple(scored[:limit])
