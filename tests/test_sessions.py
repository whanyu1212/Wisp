from __future__ import annotations

import os
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from wisp.agent.messages import Message, SessionEntry
from wisp.sessions import jsonl as jsonl_module
from wisp.sessions.jsonl import AmbiguousSessionError, JsonlSessionStore, SessionNotFoundError


def test_session_store_loads_by_path_filename_and_id_prefix(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def write() -> None:
        await session.append_message(Message(role="user", content="hello"))

    anyio.run(write)

    assert store.load(session.path).path == session.path
    assert store.load(session.path.name).path == session.path
    assert store.load(session.session_id[:12]).path == session.path
    assert store.load(session.session_id[:12]).read_messages()[0].content == "hello"


def test_session_store_opens_latest_session(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    older = store.create()
    newer = store.create()

    async def write() -> None:
        await older.append_message(Message(role="user", content="old"))
        await newer.append_message(Message(role="user", content="new"))

    anyio.run(write)
    os.utime(older.path, (1, 1))
    os.utime(newer.path, (2, 2))

    assert store.latest().path == newer.path
    assert store.latest().read_messages()[0].content == "new"


def test_session_store_reports_missing_and_ambiguous_refs(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    session_one = store.create()
    session_two = store.create()

    async def write() -> None:
        await session_one.append_message(Message(role="user", content="one"))
        await session_two.append_message(Message(role="user", content="two"))

    anyio.run(write)

    with pytest.raises(SessionNotFoundError, match="Session not found"):
        store.load("missing")
    with pytest.raises(AmbiguousSessionError, match="ambiguous"):
        store.load("20")


def test_limited_session_read_stops_after_requested_entry(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    path = tmp_path / "session.jsonl"
    path.touch()
    first_line = SessionEntry(
        session_id="session-id",
        message=Message(role="user", content="hello"),
    ).model_dump_json()

    class TrackingFile:
        next_calls = 0

        def __enter__(self) -> TrackingFile:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self) -> TrackingFile:
            return self

        def __next__(self) -> str:
            self.next_calls += 1
            if self.next_calls == 1:
                return f"{first_line}\n"
            raise AssertionError("limited read consumed another line")

    tracking_file = TrackingFile()

    def fake_open(self: Path, *args: object, **kwargs: object) -> TrackingFile:
        assert self == path
        return tracking_file

    monkeypatch.setattr(Path, "open", fake_open)

    entries = jsonl_module._read_entries(path, limit=1)  # noqa: SLF001

    assert len(entries) == 1
    assert entries[0].session_id == "session-id"
    assert tracking_file.next_calls == 1
