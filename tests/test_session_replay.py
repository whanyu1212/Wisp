from __future__ import annotations

import multiprocessing
from pathlib import Path
from typing import Any

import anyio
import pytest

from wisp.agent.messages import CompactionRecord, Message, SessionEntry
from wisp.agent.transcript import plan_interrupted_tool_repairs
from wisp.events import ToolCallSnapshot
from wisp.sessions.jsonl import JsonlSessionStore, SessionError
from wisp.sessions.replay import (
    HISTORICAL_CONTEXT_SUMMARY_LABEL,
    SessionReplayError,
    StaleCompactionError,
    replay_session_entries,
)

SESSION_ID = "session-id"


def _append_compaction_in_process(
    session_path: str,
    entry_json: str,
    expected_context_entry_ids: tuple[str, ...],
    start_event: Any,
) -> None:
    start_event.wait()
    session = JsonlSessionStore(Path(session_path).parent).load(Path(session_path))
    entry = SessionEntry.model_validate_json(entry_json)

    async def append() -> None:
        await session.append_compaction_entry(
            entry,
            expected_context_entry_ids=expected_context_entry_ids,
        )

    try:
        anyio.run(append)
    except StaleCompactionError:
        raise SystemExit(2) from None


def _message_entry(
    entry_id: str,
    role: str,
    content: str,
    **message_fields: object,
) -> SessionEntry:
    return SessionEntry.model_validate(
        {
            "id": entry_id,
            "session_id": SESSION_ID,
            "message": {"role": role, "content": content, **message_fields},
        }
    )


def _compaction_entry(
    entry_id: str,
    *replaced_entry_ids: str,
    summary: str = "Earlier work was summarized.",
) -> SessionEntry:
    return SessionEntry(
        id=entry_id,
        session_id=SESSION_ID,
        kind="compaction",
        compaction=CompactionRecord(
            summary=summary,
            replaced_entry_ids=replaced_entry_ids,
            provider="openai",
        ),
    )


def test_jsonl_raw_messages_remain_audit_while_context_uses_summary_and_suffix(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    system = SessionEntry(
        id="system",
        session_id=session.session_id,
        message=Message(role="system", content="system prompt"),
    )
    first = SessionEntry(
        id="first",
        session_id=session.session_id,
        message=Message(role="user", content="old question"),
    )
    answer = SessionEntry(
        id="answer",
        session_id=session.session_id,
        message=Message(role="assistant", content="old answer"),
    )
    compaction = SessionEntry(
        id="compact",
        session_id=session.session_id,
        kind="compaction",
        compaction=CompactionRecord(
            summary="The old question was answered.",
            replaced_entry_ids=("first", "answer"),
            provider="openai",
        ),
    )
    suffix = SessionEntry(
        id="suffix",
        session_id=session.session_id,
        message=Message(role="user", content="new question"),
    )
    suffix_answer = SessionEntry(
        id="suffix-answer",
        session_id=session.session_id,
        message=Message(role="assistant", content="new answer"),
    )

    async def write_entries() -> None:
        await session.append_entry(system)
        await session.append_entry(first)
        await session.append_entry(answer)
        await session.append_entry(
            SessionEntry(
                id="event",
                session_id=session.session_id,
                kind="event",
                event={"type": "audit.event"},
            )
        )
        await session.append_entry(suffix)
        await session.append_entry(suffix_answer)
        await session.append_compaction_entry(
            compaction,
            expected_context_entry_ids=("first", "answer", "suffix", "suffix-answer"),
        )

    anyio.run(write_entries)

    assert [message.content for message in session.read_messages()] == [
        "system prompt",
        "old question",
        "old answer",
        "new question",
        "new answer",
    ]
    replay = session.read_context()
    assert replay.context_entry_ids == ("compact", "suffix", "suffix-answer")
    assert replay.rows[0].entry_id == "compact"
    assert replay.rows[0].source_kind == "compaction"
    assert replay.messages[0].role == "user"
    assert replay.messages[0].content == (
        f"{HISTORICAL_CONTEXT_SUMMARY_LABEL}\n\nThe old question was answered."
    )
    assert [message.content for message in session.read_context_messages()] == [
        replay.messages[0].content,
        "new question",
        "new answer",
    ]


def test_replay_supports_repeated_compaction_and_new_messages() -> None:
    entries = (
        _message_entry("user-1", "user", "first"),
        _message_entry("assistant-1", "assistant", "answer"),
        _message_entry("user-2", "user", "second"),
        _message_entry("assistant-2", "assistant", "second answer"),
        _compaction_entry("compact-1", "user-1", "assistant-1", summary="First summary."),
        _message_entry("user-3", "user", "third"),
        _message_entry("assistant-3", "assistant", "third answer"),
        _compaction_entry(
            "compact-2",
            "compact-1",
            "user-2",
            "assistant-2",
            summary="Updated summary.",
        ),
        _message_entry("user-4", "user", "fourth"),
    )

    replay = replay_session_entries(entries)

    assert replay.context_entry_ids == (
        "compact-2",
        "user-3",
        "assistant-3",
        "user-4",
    )
    assert [message.content for message in replay.messages] == [
        f"{HISTORICAL_CONTEXT_SUMMARY_LABEL}\n\nUpdated summary.",
        "third",
        "third answer",
        "fourth",
    ]


@pytest.mark.parametrize(
    ("entries", "error"),
    [
        (
            (
                _message_entry("first", "user", "one"),
                _compaction_entry("compact", "first", "first"),
            ),
            "duplicate",
        ),
        (
            (
                _message_entry("first", "user", "one"),
                _compaction_entry("compact", "missing"),
            ),
            "unknown",
        ),
        (
            (
                _message_entry("first", "user", "one"),
                _message_entry("second", "assistant", "two"),
                _compaction_entry("compact", "second"),
            ),
            "prefix",
        ),
        (
            (
                _message_entry("first", "user", "one"),
                _message_entry("first-answer", "assistant", "answer one"),
                _message_entry("second", "user", "two"),
                _message_entry("second-answer", "assistant", "answer two"),
                _compaction_entry("compact-1", "first", "first-answer"),
                _compaction_entry("compact-2", "first"),
            ),
            "inactive",
        ),
    ],
)
def test_replay_rejects_invalid_compaction_targets(
    entries: tuple[SessionEntry, ...],
    error: str,
) -> None:
    with pytest.raises(SessionReplayError, match=error):
        replay_session_entries(entries)


def test_atomic_compaction_append_rejects_stale_plan_and_remains_idempotent(
    tmp_path: Path,
) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    first = SessionEntry(
        id="first",
        session_id=session.session_id,
        message=Message(role="user", content="one"),
    )
    second = SessionEntry(
        id="second",
        session_id=session.session_id,
        message=Message(role="assistant", content="two"),
    )
    retained = SessionEntry(
        id="retained",
        session_id=session.session_id,
        message=Message(role="user", content="three"),
    )
    retained_answer = SessionEntry(
        id="retained-answer",
        session_id=session.session_id,
        message=Message(role="assistant", content="four"),
    )
    compaction = SessionEntry(
        id="compact",
        session_id=session.session_id,
        kind="compaction",
        compaction=CompactionRecord(
            summary="First turn.",
            replaced_entry_ids=("first", "second"),
            provider="openai",
        ),
    )

    async def write() -> None:
        await session.append_entry(first)
        reopened = store.load(session.path)
        await reopened.append_entry(second)
        await reopened.append_entry(retained)
        await reopened.append_entry(retained_answer)
        with pytest.raises(StaleCompactionError, match="stale") as exc_info:
            await session.append_compaction_entry(
                compaction,
                expected_context_entry_ids=("first",),
            )
        assert isinstance(exc_info.value, SessionError)
        await session.append_compaction_entry(
            compaction,
            expected_context_entry_ids=("first", "second", "retained", "retained-answer"),
        )
        await reopened.append_message(Message(role="user", content="later"))
        await session.append_compaction_entry(
            compaction,
            expected_context_entry_ids=("outdated",),
        )

    anyio.run(write)

    assert [entry.id for entry in session.read_entries()].count("compact") == 1


def test_atomic_compaction_append_serializes_competing_processes(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def seed() -> tuple[str, ...]:
        first = await session.append_message(Message(role="user", content="one"))
        first_answer = await session.append_message(Message(role="assistant", content="answer"))
        second = await session.append_message(Message(role="user", content="two"))
        second_answer = await session.append_message(
            Message(role="assistant", content="answer two")
        )
        return first.id, first_answer.id, second.id, second_answer.id

    context_ids = anyio.run(seed)
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    entries = (
        _compaction_entry("compact-a", *context_ids[:2]).model_copy(
            update={"session_id": session.session_id}
        ),
        _compaction_entry("compact-b", *context_ids[:2]).model_copy(
            update={"session_id": session.session_id}
        ),
    )
    processes = [
        context.Process(
            target=_append_compaction_in_process,
            args=(
                str(session.path),
                entry.model_dump_json(exclude_none=True),
                context_ids,
                start_event,
            ),
        )
        for entry in entries
    ]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=10)
        assert not process.is_alive()

    assert sorted(process.exitcode for process in processes) == [0, 2]
    replay = session.read_context()
    assert replay.context_entry_ids[0] in {"compact-a", "compact-b"}
    assert len(tuple(entry for entry in session.read_entries() if entry.kind == "compaction")) == 1


def test_replay_preserves_tool_result_order_and_nearest_call_entry_ids() -> None:
    first_call = _message_entry(
        "call-1-first",
        "assistant",
        "",
        tool_calls=(ToolCallSnapshot(call_id="call-1", name="read", arguments={}),),
    )
    retry = _message_entry("retry", "user", "retry")
    second_call = _message_entry(
        "call-1-second",
        "assistant",
        "",
        tool_calls=(ToolCallSnapshot(call_id="call-1", name="read", arguments={}),),
    )
    later = _message_entry("later", "user", "later")
    result = _message_entry(
        "result",
        "tool",
        "done",
        tool_call_id="call-1",
        tool_name="read",
    )
    entries = (first_call, retry, second_call, later, result)

    replay = replay_session_entries(entries)
    repair = plan_interrupted_tool_repairs(
        tuple(entry.message for entry in entries if entry.message is not None)
    )

    assert replay.context_entry_ids == (
        "call-1-first",
        "retry",
        "call-1-second",
        "result",
        "later",
    )
    assert [message for message in repair.messages if message not in repair.repairs] == list(
        replay.messages
    )


def test_replay_rejects_compaction_that_splits_a_turn() -> None:
    entries = (
        _message_entry("user-1", "user", "first"),
        _message_entry("assistant-1", "assistant", "answer"),
        _message_entry("user-2", "user", "second"),
        _message_entry("assistant-2", "assistant", "answer two"),
        _compaction_entry("compact", "user-1", "assistant-1", "user-2"),
    )

    with pytest.raises(SessionReplayError, match="splits a conversation turn"):
        replay_session_entries(entries)


def test_replay_rejects_compaction_that_retains_a_truncated_turn() -> None:
    entries = (
        _message_entry("user-1", "user", "first"),
        _message_entry("assistant-1", "assistant", "answer", finish_reason="stop"),
        _message_entry("user-2", "user", "second"),
        _message_entry("assistant-2", "assistant", "partial", finish_reason="length"),
        _compaction_entry("compact", "user-1", "assistant-1"),
    )

    with pytest.raises(SessionReplayError, match="retain a complete user turn"):
        replay_session_entries(entries)


def test_replay_rejects_compaction_that_splits_tool_call_and_result() -> None:
    call = ToolCallSnapshot(call_id="call-1", name="read", arguments={})
    entries = (
        _message_entry("user-1", "user", "first"),
        _message_entry(
            "call",
            "assistant",
            "",
            tool_calls=(call,),
            finish_reason="tool_calls",
        ),
        _message_entry(
            "result",
            "tool",
            "contents",
            tool_call_id="call-1",
            tool_name="read",
        ),
        _message_entry("assistant-1", "assistant", "done"),
        _message_entry("user-2", "user", "second"),
        _message_entry("assistant-2", "assistant", "answer two"),
        _compaction_entry("compact", "user-1", "call"),
    )

    with pytest.raises(SessionReplayError, match="splits a conversation turn"):
        replay_session_entries(entries)
