"""Bounded session discovery and stable cursor contracts."""

import os
from collections.abc import Callable
from pathlib import Path

import anyio
import pytest

from wisp.agent.messages import Message
from wisp.rpc.commands import GetSessionsCommand
from wisp.rpc.framing import RpcFrameError, encode_rpc_frame
from wisp.rpc.session.read import _session_catalog_report
from wisp.sessions.catalog import read_catalog_page
from wisp.sessions.errors import SessionError
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore, SessionSummary
from wisp.sessions.summaries import read_session_summary_metadata


def seed(root: Path, count: int) -> tuple[JsonlSessionStore, list[JsonlSession]]:
    store = JsonlSessionStore(root)
    sessions = [store.create() for _ in range(count)]

    async def write() -> None:
        for index, session in enumerate(sessions):
            await session.append_message(Message(role="user", content="not searchable"))
            if index % 2:
                await session.set_name("Straße duplicate")
            os.utime(session.path, ns=(1_800_000_000_000_000_000 + index,) * 2)

    anyio.run(write)
    return store, sessions


def test_catalog_walks_more_than_200_sessions_in_both_directions(tmp_path: Path) -> None:
    store, sessions = seed(tmp_path, 207)
    page = store.catalog_page(limit=50)
    ids = [item.session_id for item in page.sessions]
    assert page.previous_cursor is None
    first = page
    second = store.catalog_page(limit=50, cursor=page.next_cursor)
    assert store.catalog_page(limit=50, cursor=second.previous_cursor) == first
    while page.next_cursor:
        page = store.catalog_page(limit=50, cursor=page.next_cursor)
        ids.extend(item.session_id for item in page.sessions)
    assert ids == [session.session_id for session in reversed(sessions)]
    assert store.catalog_page(limit=0).sessions == ()


def test_search_literal_unicode_names_ids_and_renames(tmp_path: Path) -> None:
    store, sessions = seed(tmp_path, 5)
    page = store.catalog_page(query=" STRASSE ", limit=1)
    assert page.query == "strasse"
    assert page.sessions[0].session_id == sessions[3].session_id
    assert (
        store.catalog_page(query="strasse", cursor=page.next_cursor).sessions[0].session_id
        == sessions[1].session_id
    )
    assert store.catalog_page(query=sessions[0].session_id.upper()).sessions[0].name is None
    assert store.catalog_page(query="not searchable").sessions == ()
    assert store.catalog_page(query=".*").sessions == ()
    anyio.run(sessions[3].set_name, "renamed")
    with pytest.raises(SessionError, match="changed"):
        store.catalog_page(query="strasse", cursor=page.next_cursor)
    assert len(store.catalog_page(query="strasse").sessions) == 1


def test_ties_are_deterministic_and_deletion_invalidates_cursor(tmp_path: Path) -> None:
    store, sessions = seed(tmp_path, 4)
    for session in sessions:
        os.utime(session.path, ns=(1_800_000_000_000_000_000,) * 2)
    page = store.catalog_page(limit=2)
    assert [item.path.name for item in page.sessions] == sorted(
        (session.path.name for session in sessions), reverse=True
    )[:2]
    sessions[0].path.unlink()
    with pytest.raises(SessionError, match="changed"):
        store.catalog_page(cursor=page.next_cursor)


@pytest.mark.parametrize("cursor", ["bad", "W10=", "x" * 4097])
def test_invalid_cursors(tmp_path: Path, cursor: str) -> None:
    with pytest.raises(SessionError, match="Invalid"):
        JsonlSessionStore(tmp_path).catalog_page(cursor=cursor)


def test_query_binding_bounds_and_corrupt_metadata(tmp_path: Path) -> None:
    store, _ = seed(tmp_path, 3)
    cursor = store.catalog_page(limit=1).next_cursor
    with pytest.raises(SessionError, match="Invalid"):
        store.catalog_page(query="different", cursor=cursor)
    with pytest.raises(ValueError):
        store.catalog_page(query="界" * 342)
    with pytest.raises(ValueError):
        GetSessionsCommand(query="界" * 342)
    (tmp_path / "broken.jsonl").write_text("not json\n")
    with pytest.raises(SessionError):
        store.catalog_page()


def test_unfiltered_page_only_parses_returned_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = seed(tmp_path, 20)
    read = store._summary_for_path
    seen: list[Path] = []

    def summary(path: Path, *, check_cancelled: Callable[[], None] | None = None) -> SessionSummary:
        seen.append(path)
        return read(path, check_cancelled=check_cancelled)

    monkeypatch.setattr(store, "_summary_for_path", summary)
    assert len(store.catalog_page(limit=3).sessions) == 3
    assert len(seen) == 3


def test_change_during_scan_fails_instead_of_mixing_pages(tmp_path: Path) -> None:
    store, sessions = seed(tmp_path, 2)

    def summary(path: Path) -> SessionSummary:
        os.utime(sessions[0].path, None)
        return store._summary_for_path(path)

    with pytest.raises(SessionError, match="changed"):
        read_catalog_page(
            files=store._session_files,
            summary=summary,
            limit=2,
            query="",
            cursor=None,
            check_cancelled=lambda: None,
        )


def test_summary_scan_checks_cancellation_between_lines(tmp_path: Path) -> None:
    store, sessions = seed(tmp_path, 1)

    async def append() -> None:
        for _ in range(30):
            await sessions[0].append_message(Message(role="user", content="line"))

    anyio.run(append)
    calls = 0

    def cancel() -> None:
        nonlocal calls
        calls += 1
        if calls == 10:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        read_session_summary_metadata(sessions[0].path, check_cancelled=cancel)
    assert store.catalog_page().sessions[0].entry_count == 31


def test_scan_cancellation(tmp_path: Path) -> None:
    store, _ = seed(tmp_path, 2)
    calls = 0

    def cancel() -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        store.catalog_page(check_cancelled=cancel)


def test_frame_reduction_preserves_continuation(tmp_path: Path) -> None:
    store, sessions = seed(tmp_path, 12)
    full = _session_catalog_report(
        store,
        limit=12,
        query="",
        cursor=None,
        command_id="catalog",
        selected_session=None,
        selected_session_name=None,
    )
    budget = len(encode_rpc_frame(full, max_frame_bytes=1_000_000)) // 2
    first = _session_catalog_report(
        store,
        limit=12,
        query="",
        cursor=None,
        command_id="catalog",
        selected_session=None,
        selected_session_name=None,
        max_frame_bytes=budget,
    )
    assert 0 < len(first.sessions) < 12
    rest = store.catalog_page(cursor=first.next_cursor)
    assert [s.session_id for s in (*first.sessions, *rest.sessions)] == [
        s.session_id for s in reversed(sessions)
    ]
    with pytest.raises(RpcFrameError):
        _session_catalog_report(
            store,
            limit=1,
            query="",
            cursor=None,
            command_id="catalog",
            selected_session=None,
            selected_session_name=None,
            max_frame_bytes=10,
        )
