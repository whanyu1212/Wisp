from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError

from wisp.agent.messages import CompactionRecord, Message
from wisp.events import (
    ContextBudget,
    ContextEstimate,
    ErrorEvent,
    TokenUsage,
    ToolCallSnapshot,
)
from wisp.sessions.entries import (
    ActiveLeafSessionEntry,
    CompactionSessionEntry,
    EventSessionEntry,
    MessageSessionEntry,
    PersistedEventEnvelope,
    SessionEntryAdapter,
    session_entry_to_json,
)
from wisp.sessions.errors import (
    MalformedPersistedEventError,
    MalformedSessionEntryError,
    UnsupportedPersistedEventVersionError,
    UnsupportedSessionEntryVersionError,
)
from wisp.sessions.jsonl import (
    JsonlSessionStore,
    SessionError,
)
from wisp.sessions.replay import SessionReplayError


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


def test_session_writes_versioned_discriminated_entries(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> None:
        message = await session.append_message(Message(role="user", content="hello"))
        await session.append_message(Message(role="user", content="retained"))
        await session.append_message(Message(role="assistant", content="answer"))
        await session.append_event(ErrorEvent(message="boom"))
        await session.append_entry(
            CompactionSessionEntry(
                session_id=session.session_id,
                compaction=CompactionRecord(
                    summary="Earlier context.",
                    replaced_entry_ids=(message.id,),
                    provider="openai",
                ),
            )
        )

    anyio.run(write)

    records = [json.loads(line) for line in session.path.read_text().splitlines()]
    assert [record["kind"] for record in records] == [
        "message",
        "message",
        "message",
        "event",
        "compaction",
    ]
    assert [record["schema_version"] for record in records] == [6, 6, 6, 6, 6]
    assert [record["parent_id"] for record in records] == [
        None,
        records[0]["id"],
        records[1]["id"],
        records[2]["id"],
        records[3]["id"],
    ]
    assert records[3]["event"]["schema_version"] == 1
    assert "schema_version" not in records[3]["event"]["payload"]
    assert isinstance(session.read_entries()[0], MessageSessionEntry)
    assert isinstance(session.read_entries()[3], EventSessionEntry)
    assert isinstance(session.read_entries()[4], CompactionSessionEntry)


def test_concrete_session_entry_variants_preserve_payloads() -> None:
    message = Message(role="user", content="hello")
    compaction = CompactionRecord(
        summary="Earlier context.",
        replaced_entry_ids=("message",),
        provider="openai",
    )

    message_entry = MessageSessionEntry(
        id="message",
        session_id="session",
        message=message,
    )
    event_entry = EventSessionEntry(
        id="event",
        session_id="session",
        event=PersistedEventEnvelope(
            payload={"type": "error", "schema_version": 12, "message": "boom"}
        ),
    )
    compaction_entry = CompactionSessionEntry(
        id="compaction",
        session_id="session",
        compaction=compaction,
    )

    assert isinstance(message_entry, MessageSessionEntry)
    assert message_entry.message == message
    assert isinstance(event_entry, EventSessionEntry)
    assert event_entry.event.payload["type"] == "error"
    assert isinstance(compaction_entry, CompactionSessionEntry)
    assert compaction_entry.compaction == compaction


@pytest.mark.parametrize(
    "entry",
    [
        {
            "schema_version": 6,
            "id": "message",
            "session_id": "session",
            "kind": "message",
            "message": {"role": "user", "content": "hello"},
            "created_at": "2026-07-11T00:00:00Z",
        },
        {
            "schema_version": 6,
            "id": "leaf",
            "session_id": "session",
            "kind": "active_leaf",
            "reason": "system",
            "created_at": "2026-07-11T00:00:00Z",
        },
    ],
)
def test_session_accepts_entries_with_omitted_null_structural_references(
    tmp_path: Path,
    entry: dict[str, object],
) -> None:
    path = tmp_path / "missing-reference.jsonl"
    path.write_text(f"{json.dumps(entry)}\n", encoding="utf-8")

    loaded = JsonlSessionStore(tmp_path).load(path).read_entries()[0]

    if isinstance(loaded, MessageSessionEntry):
        assert loaded.parent_id is None
    else:
        assert isinstance(loaded, ActiveLeafSessionEntry)
        assert loaded.previous_leaf_id is None
        assert loaded.active_leaf_id is None


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        (
            {
                "reason": "navigation",
                "selected_entry_id": "missing",
            },
            "unknown selected entry",
        ),
        (
            {
                "reason": "navigation",
                "selected_entry_id": "message",
                "active_leaf_id": "message",
            },
            "expected None",
        ),
        (
            {
                "reason": "unrevert",
                "source_transition_id": "missing",
            },
            "invalid navigation transition",
        ),
    ],
)
def test_session_rejects_incoherent_transition_metadata(
    tmp_path: Path,
    updates: dict[str, object],
    message: str,
) -> None:
    path = tmp_path / "incoherent-transition.jsonl"
    records = (
        {
            "schema_version": 6,
            "id": "message",
            "session_id": "session",
            "kind": "message",
            "parent_id": None,
            "message": {"role": "user", "content": "hello"},
            "created_at": "2026-07-11T00:00:00Z",
        },
        {
            "schema_version": 6,
            "id": "selection",
            "session_id": "session",
            "kind": "active_leaf",
            "previous_leaf_id": "message",
            "active_leaf_id": None,
            "created_at": "2026-07-11T00:00:01Z",
            **updates,
        },
    )
    path.write_text("".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8")

    with pytest.raises(SessionReplayError, match=message):
        JsonlSessionStore(tmp_path).load(path).read_context()
    with pytest.raises(SessionReplayError, match=message):
        JsonlSessionStore(tmp_path).summaries()


@pytest.mark.parametrize(
    "updates",
    [
        {},
        {"reason": "navigation"},
        {"reason": "unrevert", "selected_entry_id": "message"},
        {"reason": "system", "source_transition_id": "navigation"},
    ],
)
def test_session_rejects_malformed_active_leaf_metadata(
    tmp_path: Path,
    updates: dict[str, object],
) -> None:
    path = tmp_path / "active-leaf.jsonl"
    entry = {
        "schema_version": 6,
        "id": "selection",
        "session_id": "session",
        "kind": "active_leaf",
        "previous_leaf_id": None,
        "active_leaf_id": None,
        "created_at": "2026-07-11T00:00:00Z",
        **updates,
    }
    path.write_text(f"{json.dumps(entry)}\n", encoding="utf-8")

    with pytest.raises(MalformedSessionEntryError):
        JsonlSessionStore(tmp_path).load(path)
    with pytest.raises(MalformedSessionEntryError):
        JsonlSessionStore(tmp_path).summaries()


def test_session_rejects_entries_that_omit_their_parent_reference(tmp_path: Path) -> None:
    """An omitted ``parent_id`` means a root, not the previously appended entry."""

    path = tmp_path / "omitted-parent.jsonl"
    entries = (
        MessageSessionEntry(
            id="first",
            session_id="session",
            message=Message(role="user", content="one"),
        ),
        MessageSessionEntry(
            id="second",
            session_id="session",
            message=Message(role="assistant", content="two"),
        ),
    )
    path.write_text(
        "".join(f"{entry.model_dump_json(exclude_none=True)}\n" for entry in entries),
        encoding="utf-8",
    )

    with pytest.raises(SessionReplayError, match="expected active leaf 'first'"):
        JsonlSessionStore(tmp_path).load(path).read_entries()


def test_session_retains_unknown_event_payload_until_typed_access(tmp_path: Path) -> None:
    path = tmp_path / "future-event.jsonl"
    raw_event = {"type": "future.event", "future": True}
    entry = {
        "schema_version": 6,
        "id": "future-event",
        "session_id": "event-session",
        "parent_id": None,
        "kind": "event",
        "event": {"schema_version": 1, "payload": raw_event},
        "created_at": "2026-07-11T00:00:00Z",
    }
    path.write_text(f"{json.dumps(entry)}\n", encoding="utf-8")
    session = JsonlSessionStore(tmp_path).load(path)

    assert session.read_events() == (raw_event,)
    with pytest.raises(MalformedPersistedEventError, match="Malformed persisted event"):
        session.read_typed_events()


def test_session_rejects_malformed_event_only_on_typed_access(tmp_path: Path) -> None:
    path = tmp_path / "malformed-event.jsonl"
    raw_event = {"type": "error"}
    entry = {
        "schema_version": 6,
        "id": "malformed-event",
        "session_id": "event-session",
        "parent_id": None,
        "kind": "event",
        "event": {"schema_version": 1, "payload": raw_event},
        "created_at": "2026-07-11T00:00:00Z",
    }
    path.write_text(f"{json.dumps(entry)}\n", encoding="utf-8")
    session = JsonlSessionStore(tmp_path).load(path)

    assert session.read_events() == (raw_event,)
    with pytest.raises(MalformedPersistedEventError, match="Malformed persisted event"):
        session.read_typed_events()


@pytest.mark.parametrize(
    ("schema_version", "error_type", "match"),
    [
        ("1", MalformedSessionEntryError, "must be an integer"),
        (5, UnsupportedSessionEntryVersionError, "schema_version 5"),
        (7, UnsupportedSessionEntryVersionError, "schema_version 7"),
    ],
)
def test_session_distinguishes_malformed_and_future_entry_versions(
    tmp_path: Path,
    schema_version: object,
    error_type: type[SessionError],
    match: str,
) -> None:
    path = tmp_path / "entry-version.jsonl"
    entry = {
        "schema_version": schema_version,
        "id": "entry",
        "session_id": "session",
        "kind": "message",
        "message": {"role": "user", "content": "hello"},
    }
    path.write_text(f"{json.dumps(entry)}\n", encoding="utf-8")

    with pytest.raises(error_type, match=match):
        JsonlSessionStore(tmp_path).load(path)


@pytest.mark.parametrize(
    ("schema_version", "error_type", "match"),
    [
        ("1", MalformedPersistedEventError, "envelope schema_version must be an integer"),
        (2, UnsupportedPersistedEventVersionError, "envelope schema_version 2"),
    ],
)
def test_session_distinguishes_malformed_and_future_event_envelopes(
    tmp_path: Path,
    schema_version: object,
    error_type: type[SessionError],
    match: str,
) -> None:
    path = tmp_path / "event-envelope-version.jsonl"
    entry = {
        "schema_version": 6,
        "id": "event",
        "session_id": "session",
        "kind": "event",
        "created_at": "2026-07-11T00:00:00Z",
        "event": {
            "schema_version": schema_version,
            "payload": {"type": "error", "message": "boom"},
        },
    }
    path.write_text(f"{json.dumps(entry)}\n", encoding="utf-8")

    with pytest.raises(error_type, match=match):
        JsonlSessionStore(tmp_path).load(path)


def test_session_rejects_null_schema_version(
    tmp_path: Path,
) -> None:
    path = tmp_path / "null-version.jsonl"
    entry = {
        "schema_version": None,
        "id": "entry",
        "session_id": "session",
        "kind": "message",
        "message": {"role": "user", "content": "hello"},
        "created_at": "2026-07-11T00:00:00Z",
    }
    path.write_text(f"{json.dumps(entry)}\n", encoding="utf-8")

    with pytest.raises(MalformedSessionEntryError, match="must be an integer"):
        JsonlSessionStore(tmp_path).load(path)


@pytest.mark.parametrize("missing", ["id", "session_id", "created_at"])
def test_session_rejects_records_with_generated_persistence_fields(
    tmp_path: Path,
    missing: str,
) -> None:
    path = tmp_path / f"missing-{missing}.jsonl"
    entry = {
        "schema_version": 6,
        "id": "entry",
        "session_id": "session",
        "parent_id": None,
        "kind": "message",
        "message": {"role": "user", "content": "hello"},
        "created_at": "2026-07-11T00:00:00Z",
    }
    del entry[missing]
    path.write_text(f"{json.dumps(entry)}\n", encoding="utf-8")

    with pytest.raises(MalformedSessionEntryError, match=missing):
        JsonlSessionStore(tmp_path).load(path)


def test_session_rejects_entries_without_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "unversioned.jsonl"
    entry = {
        "id": "entry",
        "session_id": "session",
        "kind": "message",
        "message": {"role": "user", "content": "hello"},
        "created_at": "2026-07-11T00:00:00Z",
    }
    path.write_text(f"{json.dumps(entry)}\n", encoding="utf-8")

    with pytest.raises(UnsupportedSessionEntryVersionError, match="no schema_version"):
        JsonlSessionStore(tmp_path).load(path)
    with pytest.raises(UnsupportedSessionEntryVersionError, match="no schema_version"):
        JsonlSessionStore(tmp_path).summaries()


def test_session_rejects_extra_entry_fields(tmp_path: Path) -> None:
    path = tmp_path / "extra-field.jsonl"
    entry = {
        "schema_version": 6,
        "id": "entry",
        "session_id": "session",
        "parent_id": None,
        "kind": "message",
        "message": {"role": "user", "content": "hello"},
        "created_at": "2026-07-11T00:00:00Z",
        "unexpected": True,
    }
    path.write_text(f"{json.dumps(entry)}\n", encoding="utf-8")

    with pytest.raises(MalformedSessionEntryError, match="Malformed session entry"):
        JsonlSessionStore(tmp_path).load(path)


def test_session_rejects_mixed_session_ids_in_one_file(tmp_path: Path) -> None:
    path = tmp_path / "mixed-session-ids.jsonl"
    entries = (
        MessageSessionEntry(
            id="first",
            session_id="session-one",
            message=Message(role="user", content="first"),
        ),
        MessageSessionEntry(
            id="second",
            session_id="session-two",
            message=Message(role="assistant", content="second"),
        ),
    )
    path.write_text(
        "".join(
            f"{session_entry_to_json(entry.model_copy(update={'parent_id': parent_id}))}\n"
            for entry, parent_id in zip(entries, (None, "first"), strict=True)
        ),
        encoding="utf-8",
    )
    session = JsonlSessionStore(tmp_path).load(path)

    with pytest.raises(MalformedSessionEntryError, match="belongs to session-two"):
        session.read_entries()


def test_session_rejects_duplicate_entry_ids_in_one_file(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-entry-ids.jsonl"
    entries = (
        MessageSessionEntry(
            id="duplicate",
            session_id="session",
            message=Message(role="user", content="first"),
        ),
        MessageSessionEntry(
            id="duplicate",
            session_id="session",
            message=Message(role="assistant", content="second"),
        ),
    )
    path.write_text(
        "".join(
            f"{session_entry_to_json(entry.model_copy(update={'parent_id': parent_id}))}\n"
            for entry, parent_id in zip(entries, (None, "duplicate"), strict=True)
        ),
        encoding="utf-8",
    )
    session = JsonlSessionStore(tmp_path).load(path)

    with pytest.raises(MalformedSessionEntryError, match="Duplicate session entry id duplicate"):
        session.read_entries()


def test_session_wraps_non_integer_compaction_schema_versions(tmp_path: Path) -> None:
    path = tmp_path / "malformed-compaction.jsonl"
    entry = {
        "schema_version": 6,
        "id": "malformed",
        "session_id": "session",
        "parent_id": None,
        "kind": "compaction",
        "created_at": "2026-07-11T00:00:00Z",
        "compaction": {
            "schema_version": "4",
            "summary": "summary",
            "replaced_entry_ids": ["entry-1"],
            "provider": "openai",
        },
    }
    path.write_text(f"{json.dumps(entry)}\n", encoding="utf-8")

    with pytest.raises(MalformedSessionEntryError, match="Malformed session entry"):
        JsonlSessionStore(tmp_path).load(path).read_entries()


def test_compaction_record_is_strict_and_versioned() -> None:
    record = CompactionRecord(
        summary="Completed the investigation.",
        replaced_entry_ids=("entry-1",),
        provider="openai",
        model="gpt-5",
        instructions="Keep decisions.",
        usage=TokenUsage(input_tokens=8, output_tokens=3, total_tokens=11),
    )

    assert record.schema_version == 4
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
    budget = ContextBudget(
        estimate=ContextEstimate(
            system_tokens=1,
            message_tokens=2,
            tool_schema_tokens=0,
            total_tokens=3,
        ),
        context_window=100,
        reserve_tokens=20,
        remaining_tokens=77,
        estimated_percent=3,
        over_budget=False,
    )
    overflow = CompactionRecord(
        summary="summary",
        replaced_entry_ids=("entry-1",),
        provider="openai",
        reason="overflow",
        trigger_budget=budget,
    )
    assert overflow.reason == "overflow"
    with pytest.raises(ValidationError):
        CompactionRecord.model_validate(
            {
                "schema_version": 3,
                "summary": "summary",
                "replaced_entry_ids": ("entry-1",),
                "provider": "openai",
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
        "event": {
            "schema_version": 1,
            "payload": {"type": "event"},
        },
        "compaction": CompactionRecord(
            summary="summary",
            replaced_entry_ids=("entry-1",),
            provider="openai",
        ),
    }

    with pytest.raises(ValidationError):
        SessionEntryAdapter.validate_python(
            {
                "session_id": "session-id",
                "kind": kind,
                kind: matching[kind],
                **payloads,
            }
            if payloads
            else {"session_id": "session-id", "kind": kind}
        )
