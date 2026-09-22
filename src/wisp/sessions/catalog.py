"""Searchable catalog pages without retaining an unbounded list of summaries."""

from __future__ import annotations

import base64
import hashlib
import heapq
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from wisp.sessions.errors import SessionError

if TYPE_CHECKING:
    from wisp.sessions.jsonl import SessionSummary

MAX_CATALOG_QUERY_BYTES = 1024
MAX_CATALOG_CURSOR_BYTES = 4096


@dataclass(frozen=True, slots=True)
class SessionCatalogPage:
    """One bounded result page and opaque navigation cursors."""

    sessions: tuple[SessionSummary, ...]
    query: str
    next_cursor: str | None = None
    previous_cursor: str | None = None


def normalize_catalog_query(query: str) -> str:
    """Normalize a bounded literal name/ID query.

    Args:
        query (str): User-supplied search text.

    Returns:
        str: Trimmed, casefolded query.

    Raises:
        ValueError: The UTF-8 input exceeds the query limit.
    """
    if len(query.encode("utf-8")) > MAX_CATALOG_QUERY_BYTES:
        raise ValueError("Session query exceeds 1024 UTF-8 bytes")
    return query.strip().casefold()


def _fingerprint(paths: tuple[Path, ...], check_cancelled: Callable[[], None]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        check_cancelled()
        try:
            info = path.stat()
        except FileNotFoundError as exc:
            raise SessionError("Session catalog changed; refresh and try again") from exc
        digest.update(
            json.dumps(
                [
                    path.name,
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                ],
                ensure_ascii=True,
            ).encode()
        )
    return digest.hexdigest()


def _cursor(query: str, fingerprint: str, direction: str, key: tuple[int, str]) -> str:
    payload = json.dumps(
        [1, hashlib.sha256(query.encode()).hexdigest(), fingerprint, direction, *key],
        ensure_ascii=True,
    ).encode()
    return base64.urlsafe_b64encode(payload).decode()


def _decode_cursor(
    cursor: str, query: str, fingerprint: str
) -> tuple[Literal["next", "previous"], tuple[int, str]]:
    try:
        if len(cursor.encode("utf-8")) > MAX_CATALOG_CURSOR_BYTES:
            raise ValueError
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            not isinstance(value, list)
            or len(value) != 6
            or type(value[0]) is not int
            or value[0] != 1
            or value[1] != hashlib.sha256(query.encode()).hexdigest()
            or not isinstance(value[2], str)
            or value[3] not in ("next", "previous")
            or type(value[4]) is not int
            or not isinstance(value[5], str)
        ):
            raise ValueError
        if value[2] != fingerprint:
            raise SessionError("Session catalog changed; refresh and try again")
        direction: Literal["next", "previous"] = value[3]
        return direction, (value[4], value[5])
    except (ValueError, TypeError, UnicodeError) as exc:
        raise SessionError("Invalid session catalog cursor; refresh and try again") from exc


def read_catalog_page(
    *,
    files: Callable[[], tuple[Path, ...]],
    summary: Callable[[Path], SessionSummary],
    limit: int,
    query: str,
    cursor: str | None,
    check_cancelled: Callable[[], None],
) -> SessionCatalogPage:
    """Read a stable page, retaining at most limit plus one matching summaries.

    Directory metadata and per-file summary parsing still scale with the store;
    this is not a durable index. Cursors contain ordering keys, never paths to open.

    Args:
        files (Callable): Enumerate authorized session files in filename order.
        summary (Callable): Read one session's current metadata.
        limit (int): Maximum returned rows, from zero through 200.
        query (str): Literal name or ID search.
        cursor (str | None): Opaque cursor from a previous page.
        check_cancelled (Callable): Raise when the caller cancels.

    Returns:
        SessionCatalogPage: Newest-first results and navigation cursors.

    Raises:
        ValueError: Bounds are invalid.
        SessionError: A cursor is invalid, the catalog changes, or metadata is corrupt.
    """
    if type(limit) is not int or not 0 <= limit <= 200:
        raise ValueError("Session catalog limit must be between 0 and 200")
    normalized = normalize_catalog_query(query)
    paths = files()
    fingerprint = _fingerprint(paths, check_cancelled)
    direction, boundary = (
        _decode_cursor(cursor, normalized, fingerprint) if cursor is not None else ("next", None)
    )
    if limit == 0:
        return SessionCatalogPage((), normalized)

    def candidates() -> Iterator[tuple[tuple[int, str], Path]]:
        for path in paths:
            check_cancelled()
            try:
                key = (path.stat().st_mtime_ns, path.name)
            except FileNotFoundError as exc:
                raise SessionError("Session catalog changed; refresh and try again") from exc
            if boundary is not None and (
                (direction == "next" and key >= boundary)
                or (direction == "previous" and key <= boundary)
            ):
                continue
            yield key, path

    def matches() -> Iterator[tuple[tuple[int, str], SessionSummary]]:
        for key, path in candidates():
            item = summary(path)
            if (
                normalized in (item.name or "").casefold()
                or normalized in item.session_id.casefold()
            ):
                yield key, item

    select = heapq.nlargest if direction == "next" else heapq.nsmallest
    if normalized:
        matched = select(limit + 1, matches(), key=lambda row: row[0])
        more = len(matched) > limit
        rows = matched[:limit]
    else:
        # Unfiltered browsing needs metadata only for the returned page, not
        # every old transcript. The extra path establishes continuation.
        selected_paths = select(limit + 1, candidates(), key=lambda row: row[0])
        more = len(selected_paths) > limit
        rows = [(key, summary(path)) for key, path in selected_paths[:limit]]
    if direction == "previous":
        rows.reverse()
    if _fingerprint(files(), check_cancelled) != fingerprint:
        raise SessionError("Session catalog changed; refresh and try again")
    if not rows:
        return SessionCatalogPage((), normalized)
    has_next = more if direction == "next" else cursor is not None
    has_previous = cursor is not None if direction == "next" else more
    return SessionCatalogPage(
        tuple(row[1] for row in rows),
        normalized,
        _cursor(normalized, fingerprint, "next", rows[-1][0]) if has_next else None,
        _cursor(normalized, fingerprint, "previous", rows[0][0]) if has_previous else None,
    )
