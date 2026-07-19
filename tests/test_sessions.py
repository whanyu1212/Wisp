from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

from wisp.agent.messages import CompactionRecord, Message, SessionEntry
from wisp.events import ErrorEvent, TokenUsage, ToolCallRequested, ToolCallSnapshot
from wisp.sessions import jsonl as jsonl_module
from wisp.sessions.jsonl import (
    AmbiguousSessionError,
    JsonlSession,
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


def test_session_round_trips_completed_message_metadata(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()
    assistant = Message(
        role="assistant",
        content="running",
        tool_calls=(
            ToolCallSnapshot(
                call_id="call-1",
                name="bash",
                arguments={"command": "pwd"},
            ),
        ),
        response_id="response-1",
        finish_reason="tool_calls",
    )
    tool = Message(
        role="tool",
        content="cancelled",
        tool_call_id="call-1",
        tool_name="bash",
        is_error=True,
    )
    completed = Message(
        role="assistant",
        content="done",
        tool_calls=(),
        response_id="response-2",
        finish_reason="stop",
        usage=TokenUsage(
            input_tokens=12,
            output_tokens=7,
            total_tokens=19,
            cache_read_input_tokens=4,
            reasoning_output_tokens=3,
        ),
    )

    async def write() -> None:
        await session.append_message(assistant)
        await session.append_message(tool)
        await session.append_message(completed)

    anyio.run(write)

    assert session.read_messages() == (assistant, tool, completed)
    assert session.read_messages()[2].tool_calls == ()


def test_session_loads_legacy_messages_without_rewriting_them(tmp_path: Path) -> None:
    path = tmp_path / "legacy.jsonl"
    legacy_entry = {
        "id": "legacy-entry",
        "session_id": "legacy-session",
        "kind": "message",
        "message": {
            "role": "assistant",
            "content": "done",
            "created_at": "2026-07-11T00:00:00Z",
        },
        "created_at": "2026-07-11T00:00:00Z",
    }
    path.write_text(f"{json.dumps(legacy_entry)}\n", encoding="utf-8")
    original = path.read_bytes()

    message = JsonlSessionStore(tmp_path).load(path).read_messages()[0]

    assert message.tool_calls is None
    assert message.response_id is None
    assert message.finish_reason is None
    assert message.is_error is None
    assert message.usage is None
    assert path.read_bytes() == original


def test_session_loads_legacy_event_entries_without_rewriting_them(tmp_path: Path) -> None:
    path = tmp_path / "legacy-event.jsonl"
    legacy_entry = {
        "id": "legacy-event",
        "session_id": "legacy-session",
        "kind": "event",
        "event": {"type": "legacy.event", "value": 1},
        "created_at": "2026-07-11T00:00:00Z",
    }
    path.write_text(f"{json.dumps(legacy_entry)}\n", encoding="utf-8")
    original = path.read_bytes()

    entry = JsonlSessionStore(tmp_path).load(path).read_entries()[0]

    assert entry.event == {"type": "legacy.event", "value": 1}
    assert entry.message is None
    assert entry.compaction is None
    assert path.read_bytes() == original


def test_compaction_record_is_strict_and_versioned() -> None:
    record = CompactionRecord(
        summary="Completed the investigation.",
        replaced_entry_ids=("entry-1",),
        provider="openai",
        model="gpt-5",
        instructions="Keep decisions.",
        usage=TokenUsage(input_tokens=8, output_tokens=3, total_tokens=11),
    )

    assert record.schema_version == 2
    assert record.reason == "manual"
    assert record.replaced_entry_ids == ("entry-1",)
    with pytest.raises(ValidationError):
        CompactionRecord(
            summary="  ",
            replaced_entry_ids=("entry-1",),
            provider="openai",
        )
    with pytest.raises(ValidationError):
        CompactionRecord(
            summary="summary",
            replaced_entry_ids=(),
            provider="openai",
        )
    legacy = CompactionRecord.model_validate(
        {
            "schema_version": 1,
            "summary": "summary",
            "replaced_entry_ids": ("entry-1",),
            "provider": "openai",
        }
    )
    assert legacy.reason == "manual"
    assert legacy.trigger_budget is None
    serialized_legacy = legacy.model_dump(mode="json")
    assert "reason" not in serialized_legacy
    assert "trigger_budget" not in serialized_legacy
    with pytest.raises(ValidationError, match="v1 cannot contain v2 metadata"):
        CompactionRecord.model_validate(
            {
                "schema_version": 1,
                "summary": "summary",
                "replaced_entry_ids": ("entry-1",),
                "provider": "openai",
                "reason": "manual",
            }
        )
    with pytest.raises(ValidationError):
        CompactionRecord.model_validate(
            {
                "summary": "summary",
                "replaced_entry_ids": ("entry-1",),
                "provider": "openai",
                "unexpected": True,
            }
        )


@pytest.mark.parametrize(
    ("kind", "payloads"),
    [
        ("message", {}),
        ("message", {"event": {"type": "extra"}}),
        ("event", {"message": Message(role="user", content="extra")}),
        (
            "compaction",
            {"message": Message(role="user", content="extra")},
        ),
    ],
)
def test_session_entry_requires_exactly_its_matching_payload(
    kind: str,
    payloads: dict[str, object],
) -> None:
    matching: dict[str, object] = {
        "message": Message(role="user", content="hello"),
        "event": {"type": "event"},
        "compaction": CompactionRecord(
            summary="summary",
            replaced_entry_ids=("entry-1",),
            provider="openai",
        ),
    }

    with pytest.raises(ValidationError, match="require exactly"):
        SessionEntry.model_validate(
            {
                "session_id": "session-id",
                "kind": kind,
                kind: matching[kind],
                **payloads,
            }
            if payloads
            else {"session_id": "session-id", "kind": kind}
        )


def test_append_entry_is_idempotent(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()
    entry = SessionEntry(
        id="entry-1",
        session_id=session.session_id,
        message=Message(role="assistant", content="done"),
    )

    async def write() -> None:
        assert await session.append_entry(entry) == entry
        assert await session.append_entry(entry) == entry

    anyio.run(write)

    assert session.read_entries() == (entry,)
    assert len(session.path.read_text(encoding="utf-8").splitlines()) == 1


def test_append_entry_rechecks_identity_after_another_handle_appends(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    seed = SessionEntry(
        id="seed-entry",
        session_id=session.session_id,
        message=Message(role="user", content="start"),
    )
    entry = SessionEntry(
        id="entry-1",
        session_id=session.session_id,
        message=Message(role="assistant", content="done"),
    )

    async def write() -> None:
        await session.append_entry(seed)
        reopened = store.load(session.path)
        await reopened.append_entry(seed)
        await session.append_entry(entry)
        await reopened.append_entry(entry)

    anyio.run(write)

    assert session.read_entries() == (seed, entry)


def test_concurrent_append_entry_writes_one_record(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()
    entry = SessionEntry(
        id="entry-1",
        session_id=session.session_id,
        message=Message(role="assistant", content="done"),
    )

    async def write() -> None:
        async with anyio.create_task_group() as task_group:
            for _ in range(8):
                task_group.start_soon(session.append_entry, entry)

    anyio.run(write)

    assert session.read_entries() == (entry,)


def test_concurrent_session_handles_append_one_record(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    seed = SessionEntry(
        id="seed-entry",
        session_id=session.session_id,
        message=Message(role="user", content="start"),
    )
    entry = SessionEntry(
        id="entry-1",
        session_id=session.session_id,
        message=Message(role="assistant", content="done"),
    )
    load_entry_index = JsonlSession._load_entry_index  # noqa: SLF001

    def delayed_load_entry_index(handle: JsonlSession) -> dict[str, SessionEntry]:
        index = load_entry_index(handle)
        time.sleep(0.02)
        return index

    monkeypatch.setattr(JsonlSession, "_load_entry_index", delayed_load_entry_index)

    async def write() -> None:
        await session.append_entry(seed)
        handles = [store.load(session.path) for _ in range(8)]
        async with anyio.create_task_group() as task_group:
            for handle in handles:
                task_group.start_soon(handle.append_entry, entry)

    anyio.run(write)

    assert session.read_entries() == (seed, entry)


def test_append_entry_reloads_identity_after_uncertain_write_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    entry = SessionEntry(
        id="entry-1",
        session_id=session.session_id,
        message=Message(role="assistant", content="done"),
    )
    append_line = session._append_line  # noqa: SLF001
    should_fail = True

    def append_then_fail(line: str) -> None:
        nonlocal should_fail
        append_line(line)
        if should_fail:
            should_fail = False
            raise OSError("uncertain write outcome")

    monkeypatch.setattr(session, "_append_line", append_then_fail)

    async def write() -> None:
        with pytest.raises(OSError, match="uncertain write outcome"):
            await session.append_entry(entry)
        await session.append_entry(entry)

    anyio.run(write)

    assert session.read_entries() == (entry,)


def test_append_entry_rejects_conflicting_identity(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()
    first_message = Message(role="assistant", content="first")
    first = SessionEntry(
        id="entry-1",
        session_id=session.session_id,
        message=first_message,
    )
    conflicting = SessionEntry(
        id=first.id,
        session_id=session.session_id,
        message=first_message.model_copy(update={"content": "different"}),
        created_at=first.created_at,
    )

    async def write() -> None:
        await session.append_entry(first)
        reopened = JsonlSessionStore(tmp_path).load(session.path)
        with pytest.raises(SessionError, match="conflicts with persisted data"):
            await reopened.append_entry(conflicting)

    anyio.run(write)

    assert session.read_entries() == (first,)


def test_append_entry_rejects_another_session(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()
    entry = SessionEntry(
        id="entry-1",
        session_id="another-session",
        message=Message(role="assistant", content="done"),
    )

    async def write() -> None:
        await session.append_entry(entry)

    with pytest.raises(SessionError, match="belongs to another-session"):
        anyio.run(write)
    assert not session.path.exists()


def test_truncate_invalidates_append_identity_index(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()
    first = SessionEntry(
        id="entry-1",
        session_id=session.session_id,
        message=Message(role="user", content="first"),
    )
    second = SessionEntry(
        id="entry-2",
        session_id=session.session_id,
        message=Message(role="assistant", content="second"),
    )

    async def write() -> None:
        await session.append_entry(first)
        await session.append_entry(second)
        await session.truncate_entries(1)
        await session.append_entry(second)

    anyio.run(write)

    assert session.read_entries() == (first, second)


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


def test_append_entry_rejects_symlink_session_file(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported")
    root = tmp_path / "sessions"
    root.mkdir()
    session = JsonlSessionStore(root).create()
    entry = SessionEntry(
        id="entry-1",
        session_id=session.session_id,
        message=Message(role="assistant", content="done"),
    )
    target = tmp_path / "target.jsonl"
    target.write_text(f"{entry.model_dump_json(exclude_none=True)}\n", encoding="utf-8")
    original = target.read_bytes()
    session.path.symlink_to(target)

    async def write() -> None:
        await session.append_entry(entry)

    with pytest.raises(SessionError, match="not a regular file"):
        anyio.run(write)
    assert target.read_bytes() == original


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
