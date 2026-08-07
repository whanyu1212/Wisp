"""Project file collection and fuzzy matching for the inline ``@`` picker.

Deliberately free of Textual imports so the walker, the protected-path filter,
and the ranking are unit-testable without a pilot (see ``tests/test_tui_overlay.py``
for the same plain-fakes style).

Two responsibilities, split so they can be tested and tuned independently:

- :func:`collect_paths` walks the project once and returns the candidate corpus.
- :func:`filter_paths` ranks that corpus against a query.

Matching is *subsequence* (fzf-style), not substring: the query characters must
appear in order but need not be adjacent, so ``tuiapp`` finds
``src/wisp/tui/textual_app.py``. Bare subsequence matching is far too permissive
on its own, so :func:`score_path` supplies the precision — see its docstring.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from wisp.tools.context import ToolContext
from wisp.tools.paths import is_protected_path
from wisp.tools.search import IGNORED_DIRS

# Characters after which a match is considered to start a new "word". Matching at
# a boundary is a strong relevance signal: for the query `app`, the `a` in
# `textual_app.py` (right after `_`) means far more than the `a` inside `parser`.
_BOUNDARY_CHARS = frozenset("/_-. ")


@dataclass(frozen=True)
class FileIndexConfig:
    """Inputs governing which paths the picker may ever display.

    ``context`` carries the protected-path policy. It is a real
    :class:`~wisp.tools.context.ToolContext` rather than a bare glob tuple so the
    picker reuses ``is_protected_path`` verbatim: that matcher is subtle
    (case-insensitive, lexical *and* symlink-resolved, bare-vs-slash pattern
    semantics), and a second implementation here would be a security check free to
    drift from the one the tools enforce.
    """

    root: Path
    context: ToolContext
    # A partial index beats a hung UI on a huge repo. Both caps are deliberately
    # generous: they exist to bound pathological trees, not to trim normal ones.
    max_entries: int = 10_000
    max_depth: int = 12


@dataclass(frozen=True)
class ScoredPath:
    """One ranked candidate. ``offsets`` are match positions for highlighting."""

    path: str
    score: int
    offsets: tuple[int, ...] = field(default=())


def collect_paths(config: FileIndexConfig) -> tuple[str, ...]:
    """Walk ``config.root`` and return displayable relative paths, sorted.

    Directories are suffixed with ``/`` so the picker can distinguish them without
    re-stat-ing. Traversal prunes :data:`~wisp.tools.search.IGNORED_DIRS`, skips
    symlinked directories (a symlink loop would otherwise walk forever), and stops
    at ``max_entries``.

    Protected paths (``.env`` and friends) are excluded here rather than at display
    time. Path-only insertion means the picker never reads file *contents*, but a
    secret's *filename* is itself a disclosure, and letting one be ``@``-mentioned
    would put it in the prompt — routing around the guard that
    ``wisp.settings`` keeps user-scoped precisely so no project can disable it.
    """

    root = config.root.resolve(strict=False)
    if not root.is_dir():
        return ()

    collected: list[str] = []
    # (directory, depth); an explicit stack keeps traversal iterative so a deep
    # tree can't exhaust the interpreter's recursion limit.
    stack: list[tuple[Path, int]] = [(root, 0)]

    while stack and len(collected) < config.max_entries:
        current, depth = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError:
            # Unreadable directory (permissions, race with a delete). Skipping is
            # correct: the picker is advisory, and a partial listing beats a crash.
            continue

        for entry in entries:
            if len(collected) >= config.max_entries:
                break
            candidate = Path(entry.path)
            try:
                relative = candidate.relative_to(root)
            except ValueError:
                continue

            # follow_symlinks=False: classify the link itself, so a link to a
            # directory is never descended into (cycle guard) but is still listed.
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue

            if is_dir and entry.name in IGNORED_DIRS:
                continue
            if is_protected_path(candidate, config.context):
                continue

            if is_dir:
                collected.append(f"{relative.as_posix()}/")
                if depth + 1 < config.max_depth:
                    stack.append((candidate, depth + 1))
            else:
                collected.append(relative.as_posix())

    return tuple(sorted(collected))


def score_path(path: str, query: str) -> ScoredPath | None:
    """Score ``path`` against ``query``; ``None`` when it doesn't match.

    Greedy left-to-right subsequence match, then a weighted score. The weights are
    what make subsequence matching usable — without them ``tuiapp`` also "matches"
    ``tests/unit/parser/helper.py`` (the characters are all there, scattered) and
    the real answer is buried. The signals, in rough order of influence:

    - **Consecutive runs** — contiguous matches score quadratically, so a true
      substring hit always outranks a scattered one.
    - **Boundary starts** — a character matched right after ``/_-.`` or at a
      camelCase hump is worth far more than one mid-word.
    - **Basename over directory** — people usually remember the filename, so
      matches in the last segment are weighted above matches in the directory.
    - **Density** — a shorter path carrying the same match wins.

    Smart case, as in fzf: an all-lowercase query matches case-insensitively; any
    uppercase character in the query demands an exact-case match.
    """

    if not query:
        return ScoredPath(path=path, score=0, offsets=())

    case_sensitive = any(character.isupper() for character in query)
    haystack = path if case_sensitive else path.lower()
    needle = query if case_sensitive else query.lower()

    basename_start = path.rfind("/") + 1

    # Greedy left-to-right alignment finds *a* match but not the best one: for
    # `app` against `textual_app.py` it consumes the `a` in "textual", wrecking
    # both the contiguous run and the boundary bonus that the `a` in "app" would
    # have earned. So try every viable start for the query's first character and
    # keep the highest-scoring alignment (the same second pass fzf and VS Code
    # perform). Bounded by starts x query length — trivial at path lengths.
    best: ScoredPath | None = None
    start = haystack.find(needle[0])
    while start != -1:
        offsets = _align_from(haystack, needle, start)
        if offsets is None:
            # No alignment exists from here, so none exists from any later start.
            break
        candidate = _score_offsets(path, offsets, basename_start)
        if best is None or candidate.score > best.score:
            best = candidate
        start = haystack.find(needle[0], start + 1)

    return best


def _align_from(haystack: str, needle: str, start: int) -> tuple[int, ...] | None:
    """Greedily align ``needle`` against ``haystack`` beginning at ``start``."""

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
    """Weight one alignment. See :func:`score_path` for the signal rationale."""

    score = 0
    run_length = 0
    previous_offset = -2
    for offset in offsets:
        if offset == previous_offset + 1:
            run_length += 1
            # Quadratic in the run so long contiguous stretches dominate.
            score += 8 * run_length
        else:
            run_length = 0

        preceding = path[offset - 1] if offset > 0 else "/"
        if preceding in _BOUNDARY_CHARS:
            score += 12
        elif path[offset].isupper() and preceding.islower():
            # camelCase hump: the `S` in `SlashSuggest`.
            score += 8

        if offset >= basename_start:
            score += 6

        previous_offset = offset

    # Density: reward matches packed into a short path. Guard the divisor — an
    # empty path can't reach here, but the arithmetic shouldn't depend on that.
    span = offsets[-1] - offsets[0] + 1
    score += max(0, 30 - span)
    score += max(0, 40 - len(path)) // 4

    return ScoredPath(path=path, score=score, offsets=offsets)


def filter_paths(paths: tuple[str, ...], query: str, *, limit: int = 30) -> tuple[ScoredPath, ...]:
    """Rank ``paths`` against ``query``, best first, capped at ``limit``.

    An empty query returns the head of the corpus unranked, which is what the user
    sees the instant ``@`` is typed. Ties break on path length then lexically, so
    ordering is deterministic — the picker's tests would otherwise be flaky
    wherever two candidates score identically.

    A linear scan is deliberate. Toad reaches for a trigram index and a subinterpreter
    pool (`fuzzy_index.py`), but that is a structure for tens of thousands of paths;
    at this corpus size scoring every candidate is sub-millisecond, and the index
    would be the part most likely to be premature. This stays swappable if the
    corpus ever outgrows it.
    """

    if not query:
        return tuple(ScoredPath(path=path, score=0) for path in paths[:limit])

    scored = [result for path in paths if (result := score_path(path, query)) is not None]
    scored.sort(key=lambda result: (-result.score, len(result.path), result.path))
    return tuple(scored[:limit])
