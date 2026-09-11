"""Immutable project snapshots and fuzzy matching for the inline ``@`` picker.

The filesystem walk is deliberately free of Textual imports. It produces one
bounded, typed snapshot which the UI can safely replace as a single event-loop
operation; fuzzy matching remains a projection over the same display paths users
already see.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from wisp.project_files import (
    FileIndexConfig as FileIndexConfig,
)
from wisp.project_files import (
    ProjectChildren as ProjectChildren,
)
from wisp.project_files import (
    ProjectDirectory as ProjectDirectory,
)
from wisp.project_files import (
    ProjectEntry as ProjectEntry,
)
from wisp.project_files import (
    ProjectFile as ProjectFile,
)
from wisp.project_files import (
    ProjectSnapshot as ProjectSnapshot,
)
from wisp.project_files import (
    SnapshotTruncation as SnapshotTruncation,
)
from wisp.project_files import (
    collect_project_snapshot as collect_project_snapshot,
)

_BOUNDARY_CHARS = frozenset("/_-. ")
_JSON_DECODER = json.JSONDecoder()


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
