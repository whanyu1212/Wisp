from __future__ import annotations

from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from tests.rpc.execution_support import dispatch_parsed
from tests.support.rpc import (
    build_rpc_executor_fixture,
    guard_rpc_command_serialization,
)
from wisp.agent.messages import Message
from wisp.events import (
    ErrorEvent,
    RpcCommandFinished,
    RpcCommandStarted,
    RpcMessagesReported,
)
from wisp.sessions.entries import MessageSessionEntry
from wisp.sessions.jsonl import JsonlSession


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)


def test_executor_messages_reports_empty_without_selected_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await dispatch_parsed(executor, {"id": "messages", "type": "get_messages"})
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert result.running_command.command_type == "get_messages"
        assert completed.command_id == "messages"
        assert completed.command_type == "get_messages"
        assert completed.ok is True
        assert completed.history is None
        assert completed.entry_count == 0
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcMessagesReported,
            RpcCommandFinished,
        ]
        report = fixture.events[1]
        assert isinstance(report, RpcMessagesReported)
        assert report.session_id is None
        assert report.session_path is None
        assert report.active_leaf_id is None
        assert report.messages == ()
        assert report.truncated is False
        assert report.next_before_entry_id is None

    anyio.run(scenario)


def test_executor_messages_cancelled_before_publish_reports_failed_without_payload(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await dispatch_parsed(executor, {"id": "messages", "type": "get_messages"})
            assert result.running_command is not None
            result.running_command.cancel_scope.cancel()
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert completed.ok is False
        assert completed.history is None
        assert completed.entry_count == 0
        assert not any(isinstance(event, RpcMessagesReported) for event in fixture.events)
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_id, event.ok, event.error) for event in finished] == [
            ("messages", False, "RPC get_messages command cancelled")
        ]

    anyio.run(scenario)


def test_executor_messages_reads_selected_and_explicit_sessions(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()
        other = fixture.sessions.create()

        async def write() -> None:
            await selected.append_message(Message(role="user", content="one"))
            await selected.append_message(Message(role="assistant", content="two"))
            await selected.append_message(Message(role="user", content="three"))
            await other.append_message(Message(role="user", content="other"))

        await write()
        fixture.session_state.session = selected
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            selected_result = await dispatch_parsed(
                executor,
                {"id": "selected", "type": "get_messages", "limit": 2},
            )
            selected_completed = await receive.receive()
            explicit_result = await dispatch_parsed(
                executor,
                {
                    "id": "explicit",
                    "type": "get_messages",
                    "session_id": other.session_id,
                },
            )
            explicit_completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert selected_result.selected_session is None
        assert explicit_result.selected_session is None
        assert selected_completed.history is not None
        assert [message.content for message in selected_completed.history] == [
            "one",
            "two",
            "three",
        ]
        assert selected_completed.entry_count == 3
        assert explicit_completed.history is None
        assert explicit_completed.entry_count == 0
        reports = [event for event in fixture.events if isinstance(event, RpcMessagesReported)]
        assert [message.content for message in reports[0].messages] == ["two", "three"]
        assert reports[0].session_id == selected.session_id
        assert reports[0].session_path == selected.path
        assert reports[0].truncated is True
        assert reports[0].next_before_entry_id == reports[0].messages[0].entry_id
        assert [message.content for message in reports[1].messages] == ["other"]
        assert reports[1].session_id == other.session_id
        assert reports[1].session_path == other.path
        assert fixture.session_state.session is selected

    anyio.run(scenario)


def test_executor_messages_reuses_selected_session_index_for_explicit_pages(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()
        for index in range(4):
            await selected.append_message(Message(role="user", content=f"message-{index}"))
        fixture.session_state.session = selected
        reads = 0
        original = JsonlSession._load_entry_index

        def count_reads(session: JsonlSession) -> object:
            nonlocal reads
            reads += 1
            return original(session)

        monkeypatch.setattr(JsonlSession, "_load_entry_index", count_reads)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            await dispatch_parsed(
                executor,
                {
                    "id": "latest",
                    "type": "get_messages",
                    "session_id": selected.session_id,
                    "limit": 2,
                    "complete_structure": True,
                },
            )
            assert (await receive.receive()).ok
            initial_reads = reads
            report = next(
                event for event in fixture.events if isinstance(event, RpcMessagesReported)
            )
            await dispatch_parsed(
                executor,
                {
                    "id": "older",
                    "type": "get_messages",
                    "session_id": selected.session_id,
                    "before_entry_id": report.next_before_entry_id,
                    "limit": 2,
                    "complete_structure": True,
                },
            )
            assert (await receive.receive()).ok
            assert reads == initial_reads
            task_group.cancel_scope.cancel()

    anyio.run(scenario)


def test_executor_messages_reads_forward_after_cursor(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()
        entries = []
        for index in range(5):
            entries.append(
                await selected.append_message(Message(role="user", content=f"message-{index}"))
            )
        fixture.session_state.session = selected

        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            await dispatch_parsed(
                executor,
                {
                    "id": "newer",
                    "type": "get_messages",
                    "limit": 2,
                    "after_entry_id": entries[1].id,
                },
            )
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert completed.ok
        report = next(
            event
            for event in fixture.events
            if isinstance(event, RpcMessagesReported) and event.command_id == "newer"
        )
        assert [message.content for message in report.messages] == ["message-2", "message-3"]
        assert report.next_before_entry_id is None
        assert report.next_after_entry_id == entries[3].id

    anyio.run(scenario)


def test_executor_messages_reads_one_exact_full_content_row(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()
        exact_content = "x" * 70_000
        entry = await selected.append_message(Message(role="tool", content=exact_content))
        fixture.session_state.session = selected

        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await dispatch_parsed(
                executor,
                {
                    "id": "detail",
                    "type": "get_messages",
                    "entry_ids": [entry.id],
                    "complete_structure": True,
                    "full_content": True,
                },
            )
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert completed.ok is True
        report = next(
            event
            for event in fixture.events
            if isinstance(event, RpcMessagesReported) and event.command_id == "detail"
        )
        assert len(report.messages) == 1
        assert report.messages[0].entry_id == entry.id
        assert report.messages[0].content == exact_content
        assert report.messages[0].content_truncated is False

    anyio.run(scenario)


def test_executor_messages_historical_page_refreshes_selected_session_after_external_append(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()

        await selected.append_message(Message(role="user", content="one"))
        await selected.append_message(Message(role="assistant", content="two"))
        await selected.append_message(Message(role="user", content="three"))
        fixture.session_state.session = selected
        fixture.session_state.history = (Message(role="user", content="cached"),)
        fixture.session_state.entry_count = 3

        newest = selected.read_message_page(limit=2)
        assert newest.next_before_entry_id is not None
        entries = selected.read_entries()
        await fixture.sessions.load(selected.session_id).append_entry(
            MessageSessionEntry(
                session_id=selected.session_id,
                parent_id=entries[-1].id,
                message=Message(role="assistant", content="external"),
            )
        )

        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await dispatch_parsed(
                executor,
                {
                    "id": "older",
                    "type": "get_messages",
                    "limit": 2,
                    "before_entry_id": newest.next_before_entry_id,
                },
            )
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert completed.ok is True
        assert completed.history is not None
        assert [message.content for message in completed.history] == [
            "one",
            "two",
            "three",
            "external",
        ]
        assert completed.entry_count == 4
        reports = [event for event in fixture.events if isinstance(event, RpcMessagesReported)]
        assert [message.content for message in reports[-1].messages] == ["one"]

    anyio.run(scenario)


def test_executor_messages_reports_read_failures(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()
        await selected.append_message(Message(role="user", content="hello"))
        fixture.session_state.session = selected
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            missing = await dispatch_parsed(
                executor,
                {"id": "missing", "type": "get_messages", "session_id": "missing"},
            )
            missing_completed = await receive.receive()
            bad_cursor = await dispatch_parsed(
                executor,
                {"id": "cursor", "type": "get_messages", "before_entry_id": "missing"},
            )
            cursor_completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert missing.running_command is not None
        assert bad_cursor.running_command is not None
        assert missing_completed.ok is False
        assert cursor_completed.ok is False
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_type, event.ok, event.error) for event in finished] == [
            ("get_messages", False, "Session not found: missing"),
            ("get_messages", False, "Session message cursor not found: missing"),
        ]

    anyio.run(scenario)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        (
            "entry_ids",
            "RPC get_messages command field entry_ids must contain non-empty strings",
        ),
        (
            "complete_structure",
            "RPC get_messages complete_structure and full_content fields must be booleans",
        ),
        (
            "full_content",
            "RPC get_messages complete_structure and full_content fields must be booleans",
        ),
    ],
)
def test_executor_messages_preserves_explicit_null_rejections(
    tmp_path: Path,
    field: str,
    message: str,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {"id": "messages-null", "type": "get_messages", field: None},
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is None
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
        ]
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.command_id == "messages-null"
        assert finished.ok is False
        assert finished.error == message

    anyio.run(scenario)


def test_executor_messages_accepts_explicit_null_optional_fields(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {
                    "id": "messages-null",
                    "type": "get_messages",
                    "session_id": None,
                    "before_entry_id": None,
                    "after_entry_id": None,
                    "allow_during_prompt": None,
                },
            )
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert completed.ok is True
        report = fixture.events[1]
        assert isinstance(report, RpcMessagesReported)
        assert report.messages == ()

    anyio.run(scenario)
