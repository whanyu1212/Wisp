from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from wisp.agent.messages import Message
from wisp.events import (
    ErrorEvent,
    ToolCallRequested,
)
from wisp.sessions import (
    SessionNameChange,
    SessionTreeNavigation,
    SessionTreeNodeSummary,
    SessionTreePage,
)
from wisp.sessions import (
    jsonl as jsonl_module,
)
from wisp.sessions.entries import (
    MessageSessionEntry,
    SessionEntry,
    SessionInfoSessionEntry,
)
from wisp.sessions.errors import (
    MalformedSessionEntryError,
)
from wisp.sessions.jsonl import (
    AmbiguousSessionError,
    JsonlSessionStore,
    SessionError,
    SessionNotFoundError,
    SessionSummary,
)


def test_session_tree_facades_are_publicly_exported() -> None:
    assert SessionTreePage.__module__ == "wisp.sessions.pagination"
    assert SessionTreeNodeSummary.__module__ == "wisp.sessions.pagination"
    assert SessionTreeNavigation.__module__ == "wisp.sessions.jsonl"
    assert SessionNameChange.__module__ == "wisp.sessions.jsonl"


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


def test_session_store_summaries_return_empty_for_empty_store(tmp_path: Path) -> None:
    assert JsonlSessionStore(tmp_path).summaries() == ()


def test_session_store_summaries_are_newest_first_and_bounded(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    first = store.create()
    second = store.create()

    async def write() -> tuple[str, str]:
        first_leaf = await first.append_message(Message(role="user", content="first"))
        await second.append_message(Message(role="user", content="second"))
        second_leaf = await second.append_message(Message(role="assistant", content="answer"))
        return first_leaf.id, second_leaf.id

    first_leaf_id, second_leaf_id = anyio.run(write)
    first_mtime = 1_800_000_000
    second_mtime = first_mtime + 60
    os.utime(first.path, (first_mtime, first_mtime))
    os.utime(second.path, (second_mtime, second_mtime))

    summaries = store.summaries()

    assert [summary.session_id for summary in summaries] == [second.session_id, first.session_id]
    assert isinstance(summaries[0], SessionSummary)
    assert summaries[0].path == second.path.resolve(strict=False)
    assert summaries[0].updated_at == datetime.fromtimestamp(second_mtime, UTC)
    assert summaries[0].entry_count == 2
    assert summaries[0].active_leaf_id == second_leaf_id
    assert summaries[1].entry_count == 1
    assert summaries[1].active_leaf_id == first_leaf_id
    assert [summary.session_id for summary in store.summaries(limit=1)] == [second.session_id]
    assert store.summaries(limit=0) == ()


def test_session_store_summaries_reject_negative_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="limit must be non-negative"):
        JsonlSessionStore(tmp_path).summaries(limit=-1)


def test_session_store_summaries_propagate_malformed_session_files(tmp_path: Path) -> None:
    (tmp_path / "broken.jsonl").write_text("not-json\n", encoding="utf-8")

    with pytest.raises(SessionError):
        JsonlSessionStore(tmp_path).summaries()


def test_session_store_summaries_use_lightweight_metadata_scan(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def write() -> str:
        leaf = await session.append_message(Message(role="user", content="large payload"))
        return leaf.id

    leaf_id = anyio.run(write)

    def fail_full_entry_decode(*args: object, **kwargs: object) -> SessionEntry:
        raise AssertionError("summaries should not fully decode session entries")

    monkeypatch.setattr(jsonl_module, "session_entry_from_json", fail_full_entry_decode)

    summaries = store.summaries()

    assert len(summaries) == 1
    assert summaries[0].session_id == session.session_id
    assert summaries[0].entry_count == 1
    assert summaries[0].active_leaf_id == leaf_id


def test_session_names_are_normalized_cleared_and_latest_wins(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def write() -> tuple[SessionNameChange, SessionNameChange, SessionNameChange]:
        first = await session.set_name("  alpha\r\nbeta\n ")
        second = await session.set_name("alpha beta")
        cleared = await session.set_name(" \r\n ")
        return first, second, cleared

    first, second, cleared = anyio.run(write)

    assert first.previous_name is None
    assert first.name == "alpha beta"
    assert first.entry_count == 1
    assert second.previous_name == "alpha beta"
    assert second.name == "alpha beta"
    assert second.entry_count == 2
    assert cleared.previous_name == "alpha beta"
    assert cleared.name is None
    assert cleared.entry_count == 3
    assert session.read_name() is None
    entries = session.read_entries()
    info_entries = [entry for entry in entries if isinstance(entry, SessionInfoSessionEntry)]
    assert [entry.schema_version for entry in info_entries] == [6, 6, 6]
    assert [entry.name for entry in info_entries] == [
        "alpha beta",
        "alpha beta",
        None,
    ]


def test_session_name_limit_is_enforced_by_utf8_bytes(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def write_ok() -> None:
        await session.set_name("界" * 85 + "a")

    anyio.run(write_ok)
    assert len(session.read_name().encode("utf-8")) == 256  # type: ignore[union-attr]

    async def write_oversized() -> None:
        await session.set_name("界" * 86)

    with pytest.raises(ValueError, match="256 UTF-8 bytes"):
        anyio.run(write_oversized)


def test_session_name_metadata_does_not_affect_replay_messages_or_tree(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> str:
        first = await session.append_message(Message(role="user", content="hello"))
        await session.set_name("Named")
        await session.append_message(Message(role="assistant", content="answer"))
        await session.set_name("")
        return first.id

    first_id = anyio.run(write)

    assert [message.content for message in session.read_context_messages()] == [
        "hello",
        "answer",
    ]
    message_page = session.read_message_page()
    assert [message.content for message in message_page.messages] == ["hello", "answer"]
    tree_page = session.read_tree_page()
    assert [node.kind for node in tree_page.nodes] == ["message", "message"]
    assert tree_page.total_node_count == 2
    assert tree_page.nodes[0].entry_id == first_id


def test_metadata_only_session_is_valid_and_accepts_later_first_prompt(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> None:
        await session.set_name("Draft")
        await session.append_message(Message(role="user", content="first"))

    anyio.run(write)

    entries = session.read_entries()
    assert isinstance(entries[0], SessionInfoSessionEntry)
    assert isinstance(entries[1], MessageSessionEntry)
    assert entries[1].parent_id is None
    assert session.read_name() == "Draft"
    assert session.read_context_messages()[0].content == "first"


def test_session_summaries_expose_names_and_reject_malformed_metadata(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def write() -> None:
        await session.set_name("Named")

    anyio.run(write)

    summaries = store.summaries()

    assert summaries[0].name == "Named"

    broken = {
        "schema_version": 6,
        "id": "entry",
        "session_id": "session",
        "created_at": "2026-07-11T00:00:00Z",
        "kind": "session_info",
        "name": "界" * 86,
    }
    (tmp_path / "broken.jsonl").write_text(f"{json.dumps(broken)}\n", encoding="utf-8")

    with pytest.raises(MalformedSessionEntryError, match="256 UTF-8 bytes"):
        store.summaries()


@pytest.mark.parametrize("kind", ["message", "compaction"])
@pytest.mark.parametrize("payload", [None, "not-object"])
def test_session_store_summaries_reject_entries_missing_declared_payload(
    tmp_path: Path,
    kind: str,
    payload: object,
) -> None:
    record: dict[str, object] = {
        "schema_version": 6,
        "id": "entry",
        "session_id": "session",
        "created_at": "2026-07-11T00:00:00Z",
        "kind": kind,
        "parent_id": None,
    }
    if payload is not None:
        record[kind] = payload
    (tmp_path / "broken.jsonl").write_text(f"{json.dumps(record)}\n", encoding="utf-8")

    with pytest.raises(MalformedSessionEntryError, match="Malformed session entry"):
        JsonlSessionStore(tmp_path).summaries()


@pytest.mark.parametrize("payload", [None, "not-object"])
def test_session_store_summaries_reject_event_envelopes_missing_payload(
    tmp_path: Path,
    payload: object,
) -> None:
    event: dict[str, object] = {"schema_version": 1}
    if payload is not None:
        event["payload"] = payload
    record: dict[str, object] = {
        "schema_version": 6,
        "id": "entry",
        "session_id": "session",
        "created_at": "2026-07-11T00:00:00Z",
        "kind": "event",
        "event": event,
        "parent_id": None,
    }
    (tmp_path / "broken.jsonl").write_text(f"{json.dumps(record)}\n", encoding="utf-8")

    with pytest.raises(MalformedSessionEntryError, match="Malformed session entry"):
        JsonlSessionStore(tmp_path).summaries()


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


def test_session_recovery_preserves_latest_ordering(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    older = store.create()
    newer = store.create()

    async def write() -> None:
        await older.append_message(Message(role="user", content="old"))
        await newer.append_message(Message(role="user", content="new"))

    anyio.run(write)
    older.path.write_bytes(older.path.read_bytes() + b'{"kind":')
    os.utime(older.path, ns=(1_000_000_000, 1_000_000_000))
    os.utime(newer.path, ns=(2_000_000_000, 2_000_000_000))

    summaries = store.summaries()

    assert [summary.session_id for summary in summaries] == [newer.session_id, older.session_id]
    assert store.latest().path == newer.path
    assert older.path.stat().st_mtime_ns == 1_000_000_000


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
    first_line = MessageSessionEntry(
        session_id="session-id",
        message=Message(role="user", content="hello"),
    ).model_dump_json()
    path.write_text(f"{first_line}\n", encoding="utf-8")

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
