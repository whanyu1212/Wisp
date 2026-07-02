from __future__ import annotations

import os
import stat
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from wisp.agent.messages import Message, SessionEntry
from wisp.events import ErrorEvent, ToolCallRequested
from wisp.sessions import jsonl as jsonl_module
from wisp.sessions.jsonl import (
    AmbiguousSessionError,
    JsonlSessionStore,
    SessionError,
    SessionNotFoundError,
)


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


def test_session_persists_event_entries_without_polluting_messages(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> None:
        await session.append_message(Message(role="user", content="hello"))
        await session.append_event(
            ToolCallRequested(call_id="call-1", name="lookup", arguments={"query": "wisp"})
        )
        await session.append_event(ErrorEvent(message="boom"))
        await session.append_message(Message(role="assistant", content="done"))

    anyio.run(write)

    entries = session.read_entries()
    assert [entry.kind for entry in entries] == ["message", "event", "event", "message"]
    assert [message.content for message in session.read_messages()] == ["hello", "done"]
    events = session.read_events()
    assert [event["type"] for event in events] == ["tool.call", "error"]
    assert events[0]["call_id"] == "call-1"
    assert events[0]["name"] == "lookup"
    assert events[0]["arguments"] == {"query": "wisp"}
    assert events[1]["message"] == "boom"


def test_session_store_creates_private_directories_and_files(tmp_path: Path) -> None:
    root = tmp_path / "missing" / "sessions"
    session = JsonlSessionStore(root).create()

    async def write() -> None:
        await session.append_message(Message(role="user", content="hello"))

    anyio.run(write)

    if os.name == "posix":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(session.path.stat().st_mode) == 0o600


def test_session_store_secures_existing_session_directory(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    if os.name == "posix":
        root.chmod(0o777)
    session = JsonlSessionStore(root).create()

    async def write() -> None:
        await session.append_message(Message(role="user", content="hello"))

    anyio.run(write)

    if os.name == "posix":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(session.path.stat().st_mode) == 0o600


def test_session_store_rejects_symlink_session_directory(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported")
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "sessions"
    root.symlink_to(target, target_is_directory=True)
    session = JsonlSessionStore(root).create()

    async def write() -> None:
        await session.append_message(Message(role="user", content="hello"))

    with pytest.raises(SessionError, match="not a directory"):
        anyio.run(write)


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
