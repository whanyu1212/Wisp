from __future__ import annotations

import json
import os
import stat
import time
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

from wisp.agent.messages import CompactionRecord, Message
from wisp.agent.messages import SessionEntry as LegacySessionEntry
from wisp.events import (
    ContextBudget,
    ContextEstimate,
    ErrorEvent,
    TokenUsage,
    ToolCallRequested,
    ToolCallSnapshot,
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
    ActiveLeafSessionEntry,
    CompactionSessionEntry,
    EventSessionEntry,
    MessageSessionEntry,
    SessionEntry,
    SessionEntryAdapter,
    SessionInfoSessionEntry,
    session_entry_to_json,
)
from wisp.sessions.errors import (
    MalformedPersistedEventError,
    MalformedSessionEntryError,
    SessionNavigationCancelledError,
    StaleSessionTreeError,
    UnsupportedPersistedEventVersionError,
    UnsupportedSessionEntryVersionError,
)
from wisp.sessions.jsonl import (
    AmbiguousSessionError,
    JsonlSession,
    JsonlSessionStore,
    SessionError,
    SessionMessagePage,
    SessionNotFoundError,
    SessionSummary,
)


def _message_page_text_bytes(page: SessionMessagePage) -> int:
    total = 0
    for message in page.messages:
        total += len(message.content.encode("utf-8"))
        for tool_call in message.tool_calls:
            if not tool_call.arguments:
                continue
            rendered = json.dumps(
                tool_call.arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            total += len(rendered.encode("utf-8"))
    return total


def test_session_tree_facades_are_publicly_exported() -> None:
    assert SessionTreePage.__module__ == "wisp.sessions.jsonl"
    assert SessionTreeNodeSummary.__module__ == "wisp.sessions.jsonl"
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
    assert [entry.schema_version for entry in info_entries] == [3, 3, 3]
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
        "schema_version": 3,
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
        "schema_version": 2,
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
        "schema_version": 2,
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


def test_session_message_page_reads_active_path_messages_and_pages(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> tuple[str, str, str]:
        root = await session.append_message(Message(role="user", content="root"))
        answer = await session.append_message(Message(role="assistant", content="answer"))
        await session.append_message(Message(role="user", content="abandoned"))
        abandoned_answer = await session.append_message(
            Message(role="assistant", content="abandoned answer")
        )
        await session.select_active_leaf(
            answer.id,
            expected_active_leaf_id=abandoned_answer.id,
        )
        branch = await session.append_message(Message(role="user", content="branch"))
        await session.append_event(ErrorEvent(message="audit only"))
        await session.append_message(Message(role="assistant", content="branch answer"))
        return root.id, answer.id, branch.id

    root_id, answer_id, branch_id = anyio.run(write)

    page = session.read_message_page(limit=2)

    assert isinstance(page, SessionMessagePage)
    assert page.session_id == session.session_id
    assert page.path == session.path
    assert page.active_leaf_id != branch_id
    assert [message.entry_id for message in page.messages] == [
        branch_id,
        page.active_leaf_id,
    ]
    assert [message.content for message in page.messages] == ["branch", "branch answer"]
    assert page.messages[0].parent_id == answer_id
    assert page.truncated is True
    assert page.next_before_entry_id == branch_id

    older = session.read_message_page(limit=2, before_entry_id=branch_id)

    assert [message.entry_id for message in older.messages] == [root_id, answer_id]
    assert [message.content for message in older.messages] == ["root", "answer"]
    assert older.truncated is False
    assert older.next_before_entry_id is None


def test_session_message_page_rejects_invalid_limits_and_unknown_cursor(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> None:
        await session.append_message(Message(role="user", content="hello"))

    anyio.run(write)

    with pytest.raises(ValueError, match="at least 1"):
        session.read_message_page(limit=0)
    with pytest.raises(ValueError, match="cannot exceed 500"):
        session.read_message_page(limit=501)
    with pytest.raises(SessionError, match="Session message cursor not found"):
        session.read_message_page(before_entry_id="missing")


def test_session_tree_page_returns_empty_identified_reserved_session(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()

    page = session.read_tree_page()

    assert page == SessionTreePage(
        session_id=session.session_id,
        path=session.path,
        active_leaf_id=None,
        total_node_count=0,
        nodes=(),
        truncated=False,
        next_after_entry_id=None,
    )
    assert not session.path.exists()


def test_session_tree_page_includes_inactive_nodes_and_pages_in_append_order(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    long_prompt = "🙂" * 200

    async def write() -> tuple[str, ...]:
        root = await session.append_message(Message(role="user", content=long_prompt))
        answer = await session.append_message(Message(role="assistant", content="answer"))
        abandoned = await session.append_message(Message(role="user", content="abandoned"))
        abandoned_answer = await session.append_message(
            Message(
                role="assistant",
                content="",
                tool_calls=(
                    ToolCallSnapshot(call_id="call-1", name="read", arguments={"path": "secret"}),
                ),
                finish_reason="tool_calls",
            )
        )
        await session.select_active_leaf(
            answer.id,
            expected_active_leaf_id=abandoned_answer.id,
        )
        branch = await session.append_message(Message(role="user", content="branch"))
        event = await session.append_event(ErrorEvent(message="hidden event payload"))
        branch_answer = await session.append_message(
            Message(role="assistant", content="branch answer")
        )
        return (
            root.id,
            answer.id,
            abandoned.id,
            abandoned_answer.id,
            branch.id,
            event.id,
            branch_answer.id,
        )

    node_ids = anyio.run(write)

    first = session.read_tree_page(limit=4)
    second = session.read_tree_page(limit=4, after_entry_id=first.next_after_entry_id)
    exhausted = session.read_tree_page(limit=4, after_entry_id=node_ids[-1])

    assert [node.entry_id for node in first.nodes] == list(node_ids[:4])
    assert [node.entry_id for node in second.nodes] == list(node_ids[4:])
    assert first.total_node_count == second.total_node_count == 7
    assert first.active_leaf_id == second.active_leaf_id == node_ids[-1]
    assert first.truncated is True
    assert first.next_after_entry_id == node_ids[3]
    assert second.truncated is False
    assert second.next_after_entry_id is None
    assert exhausted.total_node_count == 7
    assert exhausted.nodes == ()
    assert exhausted.truncated is False
    assert exhausted.next_after_entry_id is None
    assert first.nodes[0].preview_truncated is True
    assert len(first.nodes[0].preview.encode("utf-8")) <= 512
    assert first.nodes[0].preview.encode("utf-8").decode("utf-8") == first.nodes[0].preview
    assert first.nodes[3].preview == "[tool calls: read]"
    assert "secret" not in first.nodes[3].preview
    assert second.nodes[1].preview == "error"
    assert "hidden event payload" not in second.nodes[1].preview


def test_session_tree_compaction_preview_is_utf8_bounded() -> None:
    entry = CompactionSessionEntry(
        session_id="session-1",
        compaction=CompactionRecord(
            summary="漢🙂" * 300,
            replaced_entry_ids=("entry-1",),
            provider="fake",
        ),
    )

    node = jsonl_module._session_tree_node_summary(entry)  # noqa: SLF001

    assert node.kind == "compaction"
    assert node.role is None
    assert node.preview_truncated is True
    assert len(node.preview.encode("utf-8")) <= 512
    assert node.preview.encode("utf-8").decode("utf-8") == node.preview


def test_session_tree_page_rejects_invalid_limits_and_unknown_cursor(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> None:
        await session.append_message(Message(role="user", content="hello"))

    anyio.run(write)

    with pytest.raises(ValueError, match="between 1 and 500"):
        session.read_tree_page(limit=0)
    with pytest.raises(ValueError, match="between 1 and 500"):
        session.read_tree_page(limit=501)
    with pytest.raises(ValueError, match="cursor must be non-empty"):
        session.read_tree_page(after_entry_id="")
    with pytest.raises(SessionError, match="Session tree cursor not found: missing"):
        session.read_tree_page(after_entry_id="missing")


def test_session_tree_navigation_restores_user_prompt_and_selects_other_nodes(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    exact_prompt = "edit this 🙂\nwithout clipping" * 100

    async def navigate() -> tuple[str, str, str]:
        root = await session.append_message(Message(role="user", content="root"))
        answer = await session.append_message(Message(role="assistant", content="answer"))
        selected = await session.append_message(Message(role="user", content=exact_prompt))
        leaf = await session.append_message(Message(role="assistant", content="old answer"))

        restored = await session.navigate_tree(
            selected.id,
            expected_active_leaf_id=leaf.id,
            operation_id="navigate-user",
        )
        assert restored.selected_entry_id == selected.id
        assert restored.previous_active_leaf_id == leaf.id
        assert restored.active_leaf_id == answer.id
        assert restored.editor_text == exact_prompt
        assert restored.changed is True
        assert restored.entry_count == 5

        current = await session.navigate_tree(
            answer.id,
            expected_active_leaf_id=answer.id,
            operation_id="navigate-current",
        )
        assert current.active_leaf_id == answer.id
        assert current.editor_text is None
        assert current.changed is False
        assert current.entry_count == 5

        reset = await session.navigate_tree(
            root.id,
            expected_active_leaf_id=answer.id,
            operation_id="navigate-root",
        )
        assert reset.active_leaf_id is None
        assert reset.editor_text == "root"
        assert reset.changed is True
        assert reset.entry_count == 6
        return root.id, answer.id, leaf.id

    root_id, answer_id, old_leaf_id = anyio.run(navigate)

    assert session.read_active_leaf_id() is None
    assert session.read_context_messages() == ()
    selections = [
        entry for entry in session.read_entries() if isinstance(entry, ActiveLeafSessionEntry)
    ]
    assert [(entry.previous_leaf_id, entry.active_leaf_id) for entry in selections] == [
        (old_leaf_id, answer_id),
        (answer_id, None),
    ]
    assert [entry.operation_id for entry in selections] == [
        "navigate-user",
        "navigate-root",
    ]
    assert root_id != answer_id


def test_session_tree_navigation_rejects_missing_and_stale_entries(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def navigate() -> None:
        leaf = await session.append_message(Message(role="user", content="root"))
        with pytest.raises(SessionError, match="Session tree entry not found: missing"):
            await session.navigate_tree(
                "missing",
                expected_active_leaf_id=leaf.id,
            )
        with pytest.raises(StaleSessionTreeError, match="expected active leaf 'stale'"):
            await session.navigate_tree(
                leaf.id,
                expected_active_leaf_id="stale",
            )
        with pytest.raises(ValueError, match="entry id must be non-empty"):
            await session.navigate_tree("", expected_active_leaf_id=leaf.id)

    anyio.run(navigate)

    assert len(session.read_entries()) == 1


def test_session_tree_navigation_honors_cancellation_at_commit_boundary(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def navigate() -> None:
        selected = await session.append_message(Message(role="user", content="root"))
        active = await session.append_message(Message(role="assistant", content="answer"))
        with pytest.raises(
            SessionNavigationCancelledError,
            match="Session tree navigation cancelled",
        ):
            await session.navigate_tree(
                selected.id,
                expected_active_leaf_id=active.id,
                cancel_requested=lambda: True,
            )
        assert session.read_active_leaf_id() == active.id

    anyio.run(navigate)

    assert len(session.read_entries()) == 2


def test_session_message_page_clips_large_content_and_tool_arguments(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    content = "🙂" * 20_000
    tool_argument = "x" * 70_000

    async def write() -> None:
        await session.append_message(
            Message(
                role="assistant",
                content=content,
                tool_calls=tuple(
                    ToolCallSnapshot(
                        call_id=f"call-{index}",
                        name="bash",
                        arguments={"command": tool_argument},
                    )
                    for index in range(20)
                ),
                finish_reason="tool_calls",
            )
        )

    anyio.run(write)

    page = session.read_message_page()
    message = page.messages[0]
    tool_call = message.tool_calls[0]

    assert message.content_truncated is True
    assert message.content_original_bytes == len(content.encode("utf-8"))
    assert message.content.encode("utf-8").decode("utf-8") == message.content
    assert len(message.content.encode("utf-8")) <= 64 * 1024
    assert len(message.tool_calls) == 16
    assert message.tool_calls_original_count == 20
    assert message.tool_calls_truncated is True
    assert tool_call.arguments_truncated is True
    assert tool_call.arguments_original_bytes > 64 * 1024
    assert set(tool_call.arguments) == {"truncated_json_preview"}
    assert len(str(tool_call.arguments["truncated_json_preview"]).encode("utf-8")) <= 64 * 1024


def test_session_message_page_applies_aggregate_budget_newest_first(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    oversized = "x" * 70_000

    async def write() -> None:
        for _ in range(9):
            await session.append_message(Message(role="assistant", content=oversized))
        await session.append_message(Message(role="user", content="latest user"))
        await session.append_message(Message(role="assistant", content="latest assistant"))

    anyio.run(write)

    page = session.read_message_page(limit=11)

    assert len(page.messages) == 11
    assert page.messages[-2].content == "latest user"
    assert page.messages[-2].content_truncated is False
    assert page.messages[-1].content == "latest assistant"
    assert page.messages[-1].content_truncated is False
    assert page.messages[0].content == ""
    assert page.messages[0].content_truncated is True
    assert _message_page_text_bytes(page) <= jsonl_module.MESSAGE_PAGE_TEXT_BYTE_LIMIT


def test_session_message_page_budgets_serialized_truncated_argument_wrapper(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    escaping_argument = "\\" * 70_000

    async def write() -> None:
        await session.append_message(
            Message(
                role="assistant",
                content="",
                tool_calls=(
                    ToolCallSnapshot(
                        call_id="call-1",
                        name="bash",
                        arguments={"command": escaping_argument},
                    ),
                ),
                finish_reason="tool_calls",
            )
        )

    anyio.run(write)

    page = session.read_message_page()
    tool_call = page.messages[0].tool_calls[0]
    rendered_arguments = json.dumps(
        tool_call.arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    assert tool_call.arguments_truncated is True
    assert set(tool_call.arguments) == {"truncated_json_preview"}
    assert len(rendered_arguments.encode("utf-8")) <= jsonl_module.TOOL_ARGUMENTS_BYTE_LIMIT
    assert _message_page_text_bytes(page) <= jsonl_module.MESSAGE_PAGE_TEXT_BYTE_LIMIT


def test_session_message_page_enforces_aggregate_text_budget(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()
    content = "🙂" * 20_000
    tool_argument = "x" * 70_000

    async def write() -> None:
        for index in range(10):
            await session.append_message(
                Message(
                    role="assistant",
                    content=content,
                    tool_calls=(
                        ToolCallSnapshot(
                            call_id=f"call-{index}",
                            name="bash",
                            arguments={"command": tool_argument},
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            )

    anyio.run(write)

    page = session.read_message_page(limit=10)

    assert len(page.messages) == 10
    assert any(message.content_truncated for message in page.messages)
    assert any(
        tool_call.arguments_truncated
        for message in page.messages
        for tool_call in message.tool_calls
    )
    assert _message_page_text_bytes(page) <= jsonl_module.MESSAGE_PAGE_TEXT_BYTE_LIMIT


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

    session = JsonlSessionStore(tmp_path).load(path)
    message = session.read_messages()[0]
    page = session.read_message_page()

    assert message.tool_calls is None
    assert message.response_id is None
    assert message.finish_reason is None
    assert message.is_error is None
    assert message.usage is None
    assert [snapshot.entry_id for snapshot in page.messages] == ["legacy-entry"]
    assert [snapshot.content for snapshot in page.messages] == ["done"]
    assert page.active_leaf_id == "legacy-entry"
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

    assert isinstance(entry, EventSessionEntry)
    assert entry.event.payload == {"type": "legacy.event", "value": 1}
    assert path.read_bytes() == original


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
    assert [record["schema_version"] for record in records] == [3, 3, 3, 3, 3]
    assert [record["parent_id"] for record in records] == [
        None,
        records[0]["id"],
        records[1]["id"],
        records[2]["id"],
        records[3]["id"],
    ]
    assert records[3]["event"]["schema_version"] == 1
    assert records[3]["event"]["payload"]["schema_version"] == 21
    assert isinstance(session.read_entries()[0], MessageSessionEntry)
    assert isinstance(session.read_entries()[3], EventSessionEntry)
    assert isinstance(session.read_entries()[4], CompactionSessionEntry)


def test_legacy_session_entry_constructor_returns_concrete_variants() -> None:
    message = Message(role="user", content="hello")
    compaction = CompactionRecord(
        summary="Earlier context.",
        replaced_entry_ids=("message",),
        provider="openai",
    )

    with pytest.warns(DeprecationWarning, match="SessionEntry is deprecated"):
        message_entry = LegacySessionEntry(
            id="message",
            session_id="session",
            message=message,
        )
    with pytest.warns(DeprecationWarning, match="SessionEntry is deprecated"):
        event_entry = LegacySessionEntry(
            id="event",
            session_id="session",
            kind="event",
            event={"type": "error", "schema_version": 12, "message": "boom"},
        )
    with pytest.warns(DeprecationWarning, match="SessionEntry is deprecated"):
        compaction_entry = LegacySessionEntry(
            id="compaction",
            session_id="session",
            kind="compaction",
            compaction=compaction,
        )

    assert isinstance(message_entry, MessageSessionEntry)
    assert message_entry.message == message
    assert isinstance(event_entry, EventSessionEntry)
    assert event_entry.event.payload["type"] == "error"
    assert isinstance(compaction_entry, CompactionSessionEntry)
    assert compaction_entry.compaction == compaction


def test_session_reads_mixed_legacy_and_v2_entries_without_rewriting(tmp_path: Path) -> None:
    path = tmp_path / "mixed.jsonl"
    legacy = {
        "id": "legacy-entry",
        "session_id": "mixed-session",
        "kind": "message",
        "message": {"role": "user", "content": "old"},
        "created_at": "2026-07-11T00:00:00Z",
    }
    current = MessageSessionEntry(
        id="current-entry",
        session_id="mixed-session",
        parent_id="legacy-entry",
        message=Message(role="assistant", content="new"),
    )
    path.write_text(
        f"{json.dumps(legacy)}\n{current.model_dump_json(exclude_none=True)}\n",
        encoding="utf-8",
    )
    original = path.read_bytes()

    entries = JsonlSessionStore(tmp_path).load(path).read_entries()

    assert [entry.id for entry in entries] == ["legacy-entry", "current-entry"]
    messages = JsonlSessionStore(tmp_path).load(path).read_messages()
    assert [message.content for message in messages] == [
        "old",
        "new",
    ]
    assert path.read_bytes() == original


def test_session_upgrades_v1_entries_to_a_parent_chain_without_rewriting(tmp_path: Path) -> None:
    path = tmp_path / "v1.jsonl"
    records = (
        {
            "schema_version": 1,
            "id": "first",
            "session_id": "session",
            "kind": "message",
            "message": {"role": "user", "content": "one"},
            "created_at": "2026-07-11T00:00:00Z",
        },
        {
            "schema_version": 1,
            "id": "second",
            "session_id": "session",
            "kind": "message",
            "message": {"role": "assistant", "content": "two"},
            "created_at": "2026-07-11T00:00:01Z",
        },
    )
    path.write_text("".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8")
    original = path.read_bytes()

    entries = JsonlSessionStore(tmp_path).load(path).read_entries()

    assert [entry.schema_version for entry in entries] == [3, 3]
    assert [entry.parent_id for entry in entries if isinstance(entry, MessageSessionEntry)] == [
        None,
        "first",
    ]
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "entry",
    [
        {
            "schema_version": 2,
            "id": "message",
            "session_id": "session",
            "kind": "message",
            "message": {"role": "user", "content": "hello"},
            "created_at": "2026-07-11T00:00:00Z",
        },
        {
            "schema_version": 2,
            "id": "leaf",
            "session_id": "session",
            "kind": "active_leaf",
            "created_at": "2026-07-11T00:00:00Z",
        },
    ],
)
def test_session_accepts_v2_entries_with_omitted_null_structural_references(
    tmp_path: Path,
    entry: dict[str, object],
) -> None:
    path = tmp_path / "missing-v2-reference.jsonl"
    path.write_text(f"{json.dumps(entry)}\n", encoding="utf-8")

    loaded = JsonlSessionStore(tmp_path).load(path).read_entries()[0]

    if isinstance(loaded, MessageSessionEntry):
        assert loaded.parent_id is None
    else:
        assert isinstance(loaded, ActiveLeafSessionEntry)
        assert loaded.previous_leaf_id is None
        assert loaded.active_leaf_id is None


def test_session_rejects_v2_session_info_entries(tmp_path: Path) -> None:
    path = tmp_path / "v2-session-info.jsonl"
    entry = {
        "schema_version": 2,
        "id": "name",
        "session_id": "session",
        "kind": "session_info",
        "name": "old-version-name",
        "created_at": "2026-07-11T00:00:00Z",
    }
    path.write_text(f"{json.dumps(entry)}\n", encoding="utf-8")

    with pytest.raises(MalformedSessionEntryError, match="session entry"):
        JsonlSessionStore(tmp_path).load(path).read_entries()
    with pytest.raises(MalformedSessionEntryError, match="session entry"):
        JsonlSessionStore(tmp_path).summaries()


def test_session_reads_public_exclude_none_serialization_as_linear_chain(
    tmp_path: Path,
) -> None:
    path = tmp_path / "public-serialization.jsonl"
    with pytest.warns(DeprecationWarning, match="SessionEntry is deprecated"):
        entries = (
            LegacySessionEntry(
                id="first",
                session_id="session",
                message=Message(role="user", content="one"),
            ),
            LegacySessionEntry(
                id="second",
                session_id="session",
                message=Message(role="assistant", content="two"),
            ),
        )
    path.write_text(
        "".join(f"{entry.model_dump_json(exclude_none=True)}\n" for entry in entries),
        encoding="utf-8",
    )
    original = path.read_bytes()

    session = JsonlSessionStore(tmp_path).load(path)
    loaded = session.read_entries()

    assert [entry.parent_id for entry in loaded if isinstance(entry, MessageSessionEntry)] == [
        None,
        "first",
    ]
    assert [message.content for message in session.read_context_messages()] == ["one", "two"]
    assert path.read_bytes() == original


@pytest.mark.parametrize("version", [5, 6])
def test_session_upgrades_legacy_v5_v6_events_only_on_typed_access(
    tmp_path: Path,
    version: int,
) -> None:
    path = tmp_path / f"event-v{version}.jsonl"
    raw_event = ErrorEvent(schema_version=version, message="historical").model_dump(mode="json")
    legacy = {
        "id": f"event-{version}",
        "session_id": "event-session",
        "kind": "event",
        "event": raw_event,
        "created_at": "2026-07-11T00:00:00Z",
    }
    path.write_text(f"{json.dumps(legacy)}\n", encoding="utf-8")
    session = JsonlSessionStore(tmp_path).load(path)

    assert session.read_events() == (raw_event,)
    typed = session.read_typed_events()
    assert len(typed) == 1
    assert isinstance(typed[0], ErrorEvent)
    assert typed[0].schema_version == version
    assert typed[0].message == "historical"


def test_session_retains_future_event_payload_until_typed_access(tmp_path: Path) -> None:
    path = tmp_path / "future-event.jsonl"
    raw_event = {"type": "future.event", "schema_version": 22, "future": True}
    legacy = {
        "id": "future-event",
        "session_id": "event-session",
        "kind": "event",
        "event": raw_event,
        "created_at": "2026-07-11T00:00:00Z",
    }
    path.write_text(f"{json.dumps(legacy)}\n", encoding="utf-8")
    session = JsonlSessionStore(tmp_path).load(path)

    assert session.read_events() == (raw_event,)
    with pytest.raises(UnsupportedPersistedEventVersionError, match="schema_version 22"):
        session.read_typed_events()


def test_session_rejects_malformed_event_only_on_typed_access(tmp_path: Path) -> None:
    path = tmp_path / "malformed-event.jsonl"
    raw_event = {"type": "error", "message": "missing version"}
    legacy = {
        "id": "malformed-event",
        "session_id": "event-session",
        "kind": "event",
        "event": raw_event,
        "created_at": "2026-07-11T00:00:00Z",
    }
    path.write_text(f"{json.dumps(legacy)}\n", encoding="utf-8")
    session = JsonlSessionStore(tmp_path).load(path)

    assert session.read_events() == (raw_event,)
    with pytest.raises(MalformedPersistedEventError, match="must be an integer"):
        session.read_typed_events()


@pytest.mark.parametrize(
    ("schema_version", "error_type", "match"),
    [
        ("1", MalformedSessionEntryError, "must be an integer"),
        (4, UnsupportedSessionEntryVersionError, "schema_version 4"),
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
        "schema_version": 1,
        "id": "event",
        "session_id": "session",
        "kind": "event",
        "created_at": "2026-07-11T00:00:00Z",
        "event": {
            "schema_version": schema_version,
            "payload": {"type": "error", "schema_version": 12, "message": "boom"},
        },
    }
    path.write_text(f"{json.dumps(entry)}\n", encoding="utf-8")

    with pytest.raises(error_type, match=match):
        JsonlSessionStore(tmp_path).load(path)


def test_session_rejects_null_schema_version_instead_of_treating_it_as_legacy(
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
@pytest.mark.parametrize("versioned", [False, True])
def test_session_rejects_records_with_generated_persistence_fields(
    tmp_path: Path,
    missing: str,
    versioned: bool,
) -> None:
    path = tmp_path / f"missing-{missing}-{versioned}.jsonl"
    entry = {
        "id": "entry",
        "session_id": "session",
        "kind": "message",
        "message": {"role": "user", "content": "hello"},
        "created_at": "2026-07-11T00:00:00Z",
    }
    if versioned:
        entry["schema_version"] = 1
    del entry[missing]
    path.write_text(f"{json.dumps(entry)}\n", encoding="utf-8")

    with pytest.raises(MalformedSessionEntryError, match=missing):
        JsonlSessionStore(tmp_path).load(path)


def test_session_rejects_legacy_records_with_conflicting_payloads(tmp_path: Path) -> None:
    path = tmp_path / "conflicting-legacy.jsonl"
    entry = {
        "id": "entry",
        "session_id": "session",
        "kind": "message",
        "message": {"role": "user", "content": "hello"},
        "event": {"type": "error", "schema_version": 5, "message": "extra"},
        "created_at": "2026-07-11T00:00:00Z",
    }
    path.write_text(f"{json.dumps(entry)}\n", encoding="utf-8")

    with pytest.raises(MalformedSessionEntryError, match="exactly a message payload"):
        JsonlSessionStore(tmp_path).load(path)


def test_session_rejects_extra_fields_in_v1_entries(tmp_path: Path) -> None:
    path = tmp_path / "extra-v1.jsonl"
    entry = {
        "schema_version": 1,
        "id": "entry",
        "session_id": "session",
        "kind": "message",
        "message": {"role": "user", "content": "hello"},
        "created_at": "2026-07-11T00:00:00Z",
        "unexpected": True,
    }
    path.write_text(f"{json.dumps(entry)}\n", encoding="utf-8")

    with pytest.raises(MalformedSessionEntryError, match="Malformed session entry"):
        JsonlSessionStore(tmp_path).load(path)


def test_session_rejects_v2_tree_metadata_claimed_by_v1_entry(tmp_path: Path) -> None:
    path = tmp_path / "v1-with-parent.jsonl"
    entry = {
        "schema_version": 1,
        "id": "entry",
        "session_id": "session",
        "kind": "message",
        "parent_id": None,
        "message": {"role": "user", "content": "hello"},
        "created_at": "2026-07-11T00:00:00Z",
    }
    path.write_text(f"{json.dumps(entry)}\n", encoding="utf-8")

    with pytest.raises(MalformedSessionEntryError, match="v2 structural field"):
        JsonlSessionStore(tmp_path).load(path)


def test_session_wraps_invalid_json_as_malformed_entry(tmp_path: Path) -> None:
    path = tmp_path / "invalid.jsonl"
    path.write_text('{"kind":"message"\n', encoding="utf-8")

    with pytest.raises(MalformedSessionEntryError, match="Malformed session entry JSON"):
        JsonlSessionStore(tmp_path).load(path)


def test_session_rejects_unknown_supported_version_event_on_typed_access(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unknown-event.jsonl"
    raw_event = {"type": "unknown", "schema_version": 12}
    legacy = {
        "id": "unknown-event",
        "session_id": "event-session",
        "kind": "event",
        "event": raw_event,
        "created_at": "2026-07-11T00:00:00Z",
    }
    path.write_text(f"{json.dumps(legacy)}\n", encoding="utf-8")
    session = JsonlSessionStore(tmp_path).load(path)

    assert session.read_events() == (raw_event,)
    with pytest.raises(MalformedPersistedEventError, match="Malformed persisted event"):
        session.read_typed_events()


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
        "id": "malformed",
        "session_id": "session",
        "kind": "compaction",
        "created_at": "2026-07-11T00:00:00Z",
        "compaction": {
            "schema_version": "3",
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
        schema_version=3,
        summary="summary",
        replaced_entry_ids=("entry-1",),
        provider="openai",
        reason="overflow",
        trigger_budget=budget,
    )
    assert overflow.schema_version == 3
    assert overflow.reason == "overflow"
    with pytest.raises(ValidationError, match="v2 does not support overflow"):
        CompactionRecord(
            schema_version=2,
            summary="summary",
            replaced_entry_ids=("entry-1",),
            provider="openai",
            reason="overflow",
            trigger_budget=budget,
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


def test_append_entry_is_idempotent(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()
    entry = MessageSessionEntry(
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
    seed = MessageSessionEntry(
        id="seed-entry",
        session_id=session.session_id,
        message=Message(role="user", content="start"),
    )
    entry = MessageSessionEntry(
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

    assert session.read_entries() == (
        seed,
        entry.model_copy(update={"parent_id": seed.id}),
    )


def test_append_entry_rejects_conflicting_explicit_parent(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()
    seed = MessageSessionEntry(
        id="seed",
        session_id=session.session_id,
        message=Message(role="user", content="start"),
    )
    conflict = MessageSessionEntry(
        id="conflict",
        session_id=session.session_id,
        parent_id="other",
        message=Message(role="assistant", content="done"),
    )

    async def write() -> None:
        await session.append_entry(seed)
        with pytest.raises(SessionError, match="specifies parent 'other'"):
            await session.append_entry(conflict)

    anyio.run(write)

    assert session.read_entries() == (seed,)


def test_append_retry_rejects_same_id_with_conflicting_parent(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()
    seed = MessageSessionEntry(
        id="seed",
        session_id=session.session_id,
        message=Message(role="user", content="start"),
    )
    entry = MessageSessionEntry(
        id="entry",
        session_id=session.session_id,
        message=Message(role="assistant", content="done"),
    )

    async def write() -> None:
        await session.append_entry(seed)
        await session.append_entry(entry)
        with pytest.raises(SessionError, match="conflicts with persisted data"):
            await session.append_entry(entry.model_copy(update={"parent_id": "other"}))

    anyio.run(write)


def test_concurrent_append_entry_writes_one_record(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()
    entry = MessageSessionEntry(
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
    seed = MessageSessionEntry(
        id="seed-entry",
        session_id=session.session_id,
        message=Message(role="user", content="start"),
    )
    entry = MessageSessionEntry(
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

    assert session.read_entries() == (
        seed,
        entry.model_copy(update={"parent_id": seed.id}),
    )


def test_repeated_append_does_not_rescan_full_session_tree(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    resolve_session_tree = jsonl_module.resolve_session_tree
    resolver_calls = 0

    def counted_resolver(entries: tuple[SessionEntry, ...]) -> object:
        nonlocal resolver_calls
        resolver_calls += 1
        return resolve_session_tree(entries)

    monkeypatch.setattr(jsonl_module, "resolve_session_tree", counted_resolver)

    async def write() -> None:
        for index in range(50):
            await session.append_message(Message(role="user", content=str(index)))

    anyio.run(write)

    assert resolver_calls == 0
    assert len(session.read_entries()) == 50
    assert resolver_calls == 1


def test_append_entry_reloads_identity_after_uncertain_write_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    entry = MessageSessionEntry(
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
    first = MessageSessionEntry(
        id="entry-1",
        session_id=session.session_id,
        message=first_message,
    )
    conflicting = MessageSessionEntry(
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
    entry = MessageSessionEntry(
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
    first = MessageSessionEntry(
        id="entry-1",
        session_id=session.session_id,
        message=Message(role="user", content="first"),
    )
    second = MessageSessionEntry(
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

    assert session.read_entries() == (
        first,
        second.model_copy(update={"parent_id": first.id}),
    )


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
    entry = MessageSessionEntry(
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
    first_line = MessageSessionEntry(
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
