from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

from wisp.agent.messages import Message
from wisp.events import (
    ErrorEvent,
    RpcMessageToolResultSnapshot,
    ToolCallSnapshot,
)
from wisp.sessions import (
    jsonl as jsonl_module,
)
from wisp.sessions import (
    pagination,
)
from wisp.sessions.entries import (
    MessageSessionEntry,
    SessionEntry,
    ToolResultPresentationSnapshot,
    session_entry_to_json,
)
from wisp.sessions.jsonl import (
    JsonlSessionStore,
    SessionError,
    SessionMessagePage,
)
from wisp.sessions.replay import SessionTreeState


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
        if message.tool_result is not None and message.tool_result.before_text is not None:
            total += len(message.tool_result.before_text.encode("utf-8"))
        if message.tool_result is not None and message.tool_result.summary is not None:
            total += len(message.tool_result.summary.encode("utf-8"))
    return total


def test_reads_active_path_messages_and_pages(
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


def test_reads_forward_after_cursor(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> list[str]:
        entries = []
        for index in range(7):
            entries.append(
                await session.append_message(Message(role="user", content=f"message-{index}"))
            )
        return [entry.id for entry in entries]

    entry_ids = anyio.run(write)

    first = session.read_message_page(limit=2, after_entry_id=entry_ids[1])
    second = session.read_message_page(
        limit=2,
        after_entry_id=first.next_after_entry_id,
    )

    assert [message.content for message in first.messages] == ["message-2", "message-3"]
    assert first.truncated
    assert first.next_before_entry_id is None
    assert first.next_after_entry_id == entry_ids[3]
    assert [message.content for message in second.messages] == ["message-4", "message-5"]
    assert second.next_after_entry_id == entry_ids[5]

    with pytest.raises(ValueError, match="mutually exclusive"):
        session.read_message_page(
            before_entry_id=entry_ids[4],
            after_entry_id=entry_ids[1],
        )


def test_pages_reuse_validated_entry_index(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def write() -> None:
        for index in range(5):
            await session.append_message(Message(role="user", content=f"message-{index}"))

    anyio.run(write)
    reader = store.load(session.session_id)
    original_read_entries = jsonl_module._read_entries_unlocked
    read_count = 0

    def count_reads(path: Path, *, limit: int | None = None) -> list[SessionEntry]:
        nonlocal read_count
        read_count += 1
        return original_read_entries(path, limit=limit)

    monkeypatch.setattr(jsonl_module, "_read_entries_unlocked", count_reads)

    newest = reader.read_message_page(limit=2)
    older = reader.read_message_page(limit=2, before_entry_id=newest.next_before_entry_id)

    assert [message.content for message in newest.messages] == ["message-3", "message-4"]
    assert [message.content for message in older.messages] == ["message-1", "message-2"]
    assert read_count == 1


def test_pages_reuse_active_path_index(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def write() -> None:
        for index in range(5):
            await session.append_message(Message(role="user", content=f"message-{index}"))

    anyio.run(write)
    reader = store.load(session.session_id)
    original_resolve = jsonl_module.resolve_session_tree
    resolve_count = 0

    def count_resolves(entries: Sequence[SessionEntry]) -> SessionTreeState:
        nonlocal resolve_count
        resolve_count += 1
        return original_resolve(entries)

    # The JSONL reader validates the file; the pagination projection builds the index.
    monkeypatch.setattr(jsonl_module, "resolve_session_tree", count_resolves)
    monkeypatch.setattr(pagination, "resolve_session_tree", count_resolves)

    newest = reader.read_message_page(limit=2)
    older = reader.read_message_page(limit=2, before_entry_id=newest.next_before_entry_id)

    assert [message.content for message in newest.messages] == ["message-3", "message-4"]
    assert [message.content for message in older.messages] == ["message-1", "message-2"]
    # The initial read validates JSONL once and builds the active-path cache once.
    # Cursor reads use the cache without another tree traversal.
    assert resolve_count == 2


def test_cache_reloads_after_external_append(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def write() -> MessageSessionEntry:
        entry = await session.append_message(Message(role="user", content="first"))
        assert isinstance(entry, MessageSessionEntry)
        return entry

    first = anyio.run(write)
    reader = store.load(session.session_id)
    original_read_entries = jsonl_module._read_entries_unlocked
    read_count = 0

    def count_reads(path: Path, *, limit: int | None = None) -> list[SessionEntry]:
        nonlocal read_count
        read_count += 1
        return original_read_entries(path, limit=limit)

    monkeypatch.setattr(jsonl_module, "_read_entries_unlocked", count_reads)

    assert [message.content for message in reader.read_message_page().messages] == ["first"]
    external = MessageSessionEntry(
        session_id=session.session_id,
        parent_id=first.id,
        message=Message(role="user", content="external"),
    )
    with session.path.open("a", encoding="utf-8") as session_file:
        session_file.write(session_entry_to_json(external))
        session_file.write("\n")

    reloaded = reader.read_message_page()

    assert [message.content for message in reloaded.messages] == ["first", "external"]
    assert read_count == 2


def test_rejects_invalid_limits_and_unknown_cursor(
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


def test_clips_large_content_and_tool_arguments(
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


def test_complete_structure_preserves_every_tool_call(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    tool_argument = "x" * 70_000

    async def write() -> None:
        await session.append_message(
            Message(
                role="assistant",
                content="",
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

    message = session.read_message_page(complete_structure=True).messages[0]

    assert len(message.tool_calls) == message.tool_calls_original_count == 20
    assert message.tool_calls_truncated is False
    assert all(not tool_call.arguments_truncated for tool_call in message.tool_calls)
    assert all(
        tool_call.arguments == {"command": tool_argument} for tool_call in message.tool_calls
    )


def test_complete_structure_preserves_process_arguments(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    process_id = "a" * 32

    async def write() -> None:
        await session.append_message(
            Message(
                role="assistant",
                content="",
                tool_calls=(
                    ToolCallSnapshot(
                        call_id="poll-call",
                        name="bash",
                        arguments={
                            "operation": "poll",
                            "process_id": process_id,
                            "wait_seconds": 30,
                        },
                    ),
                ),
                finish_reason="tool_calls",
            )
        )
        for _ in range(8):
            await session.append_message(
                Message(
                    role="assistant",
                    content="x" * pagination.MESSAGE_CONTENT_BYTE_LIMIT,
                )
            )

    anyio.run(write)

    page = session.read_message_page(limit=9, complete_structure=True)
    process_call = page.messages[0].tool_calls[0]

    assert process_call.arguments == {
        "operation": "poll",
        "process_id": process_id,
        "wait_seconds": 30,
    }
    assert process_call.arguments_truncated is False
    assert all(not message.content_truncated for message in page.messages)
    assert _message_page_text_bytes(page) > pagination.MESSAGE_PAGE_TEXT_BYTE_LIMIT


def test_exact_full_content_bypasses_preview_limits(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    content = "🙂" * 20_000
    tool_argument = "x" * 70_000

    async def write() -> str:
        entry = await session.append_message(
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
        return entry.id

    entry_id = anyio.run(write)
    page = session.read_message_page(
        limit=1,
        entry_ids=(entry_id,),
        complete_structure=True,
        full_content=True,
    )
    message = page.messages[0]

    assert page.truncated is False
    assert page.next_before_entry_id is None
    assert message.content == content
    assert message.content_truncated is False
    assert len(message.tool_calls) == 20
    assert message.tool_calls_truncated is False
    assert all(
        tool_call.arguments == {"command": tool_argument} for tool_call in message.tool_calls
    )
    assert all(not tool_call.arguments_truncated for tool_call in message.tool_calls)

    with pytest.raises(SessionError, match="not found on active path"):
        session.read_message_page(entry_ids=("missing",), full_content=True)
    with pytest.raises(ValueError, match="cannot be combined"):
        session.read_message_page(entry_ids=(entry_id,), before_entry_id=entry_id)
    with pytest.raises(ValueError, match="must be unique"):
        session.read_message_page(entry_ids=(entry_id, entry_id), full_content=True)


@pytest.mark.parametrize("complete_structure", [False, True])
def test_applies_aggregate_budget_only_to_previews(
    tmp_path: Path,
    complete_structure: bool,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    oversized = "x" * 70_000

    async def write() -> None:
        for _ in range(9):
            await session.append_message(Message(role="assistant", content=oversized))
        await session.append_message(Message(role="user", content="latest user"))
        await session.append_message(Message(role="assistant", content="latest assistant"))

    anyio.run(write)

    page = session.read_message_page(limit=11, complete_structure=complete_structure)

    assert len(page.messages) == 11
    assert page.messages[-2].content == "latest user"
    assert page.messages[-2].content_truncated is False
    assert page.messages[-1].content == "latest assistant"
    assert page.messages[-1].content_truncated is False
    if complete_structure:
        assert page.messages[0].content == oversized
        assert all(not message.content_truncated for message in page.messages)
        assert _message_page_text_bytes(page) > pagination.MESSAGE_PAGE_TEXT_BYTE_LIMIT
    else:
        assert page.messages[0].content == ""
        assert page.messages[0].content_truncated is True
        assert _message_page_text_bytes(page) <= pagination.MESSAGE_PAGE_TEXT_BYTE_LIMIT


def test_budgets_serialized_truncated_argument_wrapper(
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
    assert len(rendered_arguments.encode("utf-8")) <= pagination.TOOL_ARGUMENTS_BYTE_LIMIT
    assert _message_page_text_bytes(page) <= pagination.MESSAGE_PAGE_TEXT_BYTE_LIMIT


def test_projects_tool_result_presentation_metadata(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    entry = MessageSessionEntry(
        session_id=session.session_id,
        message=Message(
            role="tool",
            content="created file",
            tool_call_id="call-1",
            tool_name="write",
            is_error=False,
        ),
        tool_result=ToolResultPresentationSnapshot(
            status="done",
            output_has_exit_status=True,
            before_text="old\n",
            created=False,
            summary="wrote file",
            truncated=True,
        ),
    )

    async def write() -> None:
        await session.append_entry(entry)

    anyio.run(write)

    reloaded = session.read_entries()[0]
    assert isinstance(reloaded, MessageSessionEntry)
    assert reloaded.tool_result == entry.tool_result

    page = session.read_message_page()
    assert page.messages[0].content_truncated is False
    assert page.messages[0].tool_result == RpcMessageToolResultSnapshot(
        status="done",
        output_has_exit_status=True,
        before_text="old\n",
        created=False,
        summary="wrote file",
        truncated=True,
    )


def test_rejects_tool_result_metadata_on_non_tool_message() -> None:
    with pytest.raises(ValidationError, match="valid only on tool messages"):
        MessageSessionEntry(
            session_id="session-1",
            message=Message(role="assistant", content="not a tool result"),
            tool_result=ToolResultPresentationSnapshot(summary="invalid"),
        )


def test_drops_partial_before_text_metadata(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()
    oversized_before_text = "x" * (pagination.MESSAGE_CONTENT_BYTE_LIMIT + 1)

    async def write() -> None:
        await session.append_entry(
            MessageSessionEntry(
                session_id=session.session_id,
                message=Message(
                    role="tool",
                    content="ok",
                    tool_call_id="call-1",
                    tool_name="write",
                    is_error=False,
                ),
                tool_result=ToolResultPresentationSnapshot(
                    before_text=oversized_before_text,
                    created=False,
                ),
            )
        )

    anyio.run(write)

    page = session.read_message_page()
    assert page.messages[0].content_truncated is True
    assert page.messages[0].tool_result is not None
    assert page.messages[0].tool_result.before_text is None
    assert page.messages[0].tool_result.truncated is True
    assert _message_page_text_bytes(page) <= pagination.MESSAGE_PAGE_TEXT_BYTE_LIMIT

    exact = session.read_message_page(
        entry_ids=(page.messages[0].entry_id,),
        full_content=True,
    )
    assert exact.messages[0].content_truncated is False
    assert exact.messages[0].tool_result is not None
    assert exact.messages[0].tool_result.before_text == oversized_before_text
    assert exact.messages[0].tool_result.truncated is False


def test_bounds_tool_result_summary_metadata(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()
    oversized_summary = "🙂" * (pagination.MESSAGE_CONTENT_BYTE_LIMIT + 1)

    async def write() -> None:
        await session.append_entry(
            MessageSessionEntry(
                session_id=session.session_id,
                message=Message(
                    role="tool",
                    content="ok",
                    tool_call_id="call-1",
                    tool_name="read",
                    is_error=False,
                ),
                tool_result=ToolResultPresentationSnapshot(summary=oversized_summary),
            )
        )

    anyio.run(write)

    page = session.read_message_page()
    assert page.messages[0].tool_result is not None
    assert page.messages[0].tool_result.summary is not None
    assert (
        len(page.messages[0].tool_result.summary.encode("utf-8"))
        <= pagination.MESSAGE_CONTENT_BYTE_LIMIT
    )
    assert page.messages[0].tool_result.truncated is True
    assert page.messages[0].content_truncated is True
    assert _message_page_text_bytes(page) <= pagination.MESSAGE_PAGE_TEXT_BYTE_LIMIT


def test_enforces_aggregate_text_budget(tmp_path: Path) -> None:
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
    assert _message_page_text_bytes(page) <= pagination.MESSAGE_PAGE_TEXT_BYTE_LIMIT
