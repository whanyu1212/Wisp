from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from tests.rpc.execution_support import dispatch_parsed
from tests.support.rpc import (
    build_rpc_executor_fixture,
    guard_rpc_command_serialization,
    preserve_running_command,
    reject_unexpected_command,
)
from wisp.events import (
    RpcCommandFinished,
    RpcCommandStarted,
    RpcSessionTreeNavigated,
    RpcSessionTreeReported,
    RpcSessionTreeUnreverted,
)
from wisp.messages import Message
from wisp.rpc.session import mutation as rpc_session_mutation_module
from wisp.sessions.jsonl import JsonlSession, SessionTreeNavigation


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)


def test_executor_session_tree_reports_empty_and_bounded_selected_pages(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            empty = await dispatch_parsed(
                executor,
                {"id": "tree-empty", "type": "get_session_tree"},
            )
            empty_completed = await receive.receive()
            fixture.coordinator.running_command = empty.running_command
            await fixture.coordinator.handle_event(
                empty_completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )

            session = fixture.sessions.create()
            fixture.session_state.session = session
            reserved = await dispatch_parsed(
                executor,
                {"id": "tree-reserved", "type": "get_session_tree"},
            )
            reserved_completed = await receive.receive()
            fixture.coordinator.running_command = reserved.running_command
            await fixture.coordinator.handle_event(
                reserved_completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            first = await session.append_message(Message(role="user", content="first"))
            await session.append_message(Message(role="assistant", content="answer"))
            fixture.session_state.history = session.read_context_messages()
            fixture.session_state.entry_count = 2
            selected = await dispatch_parsed(
                executor,
                {"id": "tree-selected", "type": "get_session_tree", "limit": 1},
            )
            selected_completed = await receive.receive()
            task_group.cancel_scope.cancel()

        reports = [event for event in fixture.events if isinstance(event, RpcSessionTreeReported)]
        assert empty_completed.ok is True
        assert reports[0].session_id is None
        assert reports[0].session_path is None
        assert reports[0].nodes == ()
        assert reports[0].total_node_count == 0
        assert reserved_completed.ok is True
        assert reports[1].session_id == session.session_id
        assert reports[1].session_path == session.path
        assert reports[1].active_leaf_id is None
        assert reports[1].nodes == ()
        assert reports[1].total_node_count == 0
        assert selected.running_command is not None
        assert selected.running_command.command_type == "get_session_tree"
        assert selected_completed.ok is True
        assert selected_completed.history == session.read_context_messages()
        assert selected_completed.entry_count == 2
        assert reports[2].session_id == session.session_id
        assert reports[2].session_path == session.path
        assert reports[2].total_node_count == 2
        assert [node.entry_id for node in reports[2].nodes] == [first.id]
        assert reports[2].truncated is True
        assert reports[2].next_after_entry_id == first.id

    anyio.run(scenario)


def test_executor_navigation_applies_history_before_reporting_success(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        session = fixture.sessions.create()
        await session.append_message(Message(role="user", content="first"))
        answer = await session.append_message(Message(role="assistant", content="answer"))
        editable = "edit this exact prompt 漢🙂"
        selected = await session.append_message(Message(role="user", content=editable))
        await session.append_message(Message(role="assistant", content="old answer"))
        fixture.session_state.session = session
        fixture.session_state.history = session.read_context_messages()
        fixture.session_state.entry_count = 4
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await dispatch_parsed(
                executor,
                {
                    "id": "navigate",
                    "type": "navigate_session_tree",
                    "entry_id": selected.id,
                },
            )
            completed = await receive.receive()
            assert not any(isinstance(event, RpcSessionTreeNavigated) for event in fixture.events)
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert result.running_command.command_type == "navigate_session_tree"
        assert completed.ok is True
        assert [message.content for message in fixture.session_state.history] == [
            "first",
            "answer",
        ]
        assert fixture.session_state.entry_count == 5
        assert session.read_active_leaf_id() == answer.id
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcSessionTreeNavigated,
            RpcCommandFinished,
        ]
        report = fixture.events[1]
        assert isinstance(report, RpcSessionTreeNavigated)
        assert report.session_id == session.session_id
        assert report.session_path == session.path
        assert report.selected_entry_id == selected.id
        assert report.previous_active_leaf_id is not None
        assert report.active_leaf_id == answer.id
        assert report.editor_text == editable
        assert report.changed is True
        assert report.entry_count == 5

    anyio.run(scenario)


def test_executor_unrevert_applies_history_before_reporting_success(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        session = fixture.sessions.create()
        await session.append_message(Message(role="user", content="first"))
        answer = await session.append_message(Message(role="assistant", content="answer"))
        selected = await session.append_message(Message(role="user", content="second"))
        leaf = await session.append_message(Message(role="assistant", content="old answer"))
        original_history = session.read_context_messages()
        await session.navigate_tree(
            selected.id,
            expected_active_leaf_id=leaf.id,
            operation_id="navigate",
        )
        fixture.session_state.session = session
        fixture.session_state.history = session.read_context_messages()
        fixture.session_state.entry_count = 5
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await dispatch_parsed(
                executor,
                {"id": "unrevert", "type": "unrevert_session_tree"},
            )
            completed = await receive.receive()
            assert not any(isinstance(event, RpcSessionTreeUnreverted) for event in fixture.events)
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert result.running_command.command_type == "unrevert_session_tree"
        assert completed.ok is True
        assert fixture.session_state.history == original_history
        assert fixture.session_state.entry_count == 6
        assert session.read_active_leaf_id() == leaf.id
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcSessionTreeUnreverted,
            RpcCommandFinished,
        ]
        report = fixture.events[1]
        assert isinstance(report, RpcSessionTreeUnreverted)
        assert report.previous_active_leaf_id == answer.id
        assert report.active_leaf_id == leaf.id
        assert report.entry_count == 6

    anyio.run(scenario)


def test_executor_unrevert_requires_selected_session_and_eligible_navigation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            assert (
                await dispatch_parsed(
                    executor,
                    {"id": "unrevert-none", "type": "unrevert_session_tree"},
                )
            ).running_command is None
            session = fixture.sessions.create()
            await session.append_message(Message(role="user", content="first"))
            fixture.session_state.session = session
            fixture.session_state.history = session.read_context_messages()
            fixture.session_state.entry_count = 1
            result = await dispatch_parsed(
                executor,
                {"id": "unrevert-missing", "type": "unrevert_session_tree"},
            )
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert completed.ok is False
        assert fixture.session_state.entry_count == 1
        assert len(session.read_entries()) == 1
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [event.error for event in finished] == [
            "RPC unrevert_session_tree command requires an existing persisted session",
            "No explicit session-tree navigation is available to unrevert",
        ]

    anyio.run(scenario)


def test_executor_unrevert_rejects_concurrent_leaf_change(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        session = fixture.sessions.create()
        await session.append_message(Message(role="user", content="first"))
        await session.append_message(Message(role="assistant", content="answer"))
        selected = await session.append_message(Message(role="user", content="second"))
        leaf = await session.append_message(Message(role="assistant", content="old answer"))
        await session.navigate_tree(selected.id, expected_active_leaf_id=leaf.id)
        previous_history = session.read_context_messages()
        previous_leaf_id = session.read_active_leaf_id()
        fixture.session_state.session = session
        fixture.session_state.history = previous_history
        fixture.session_state.entry_count = 5
        original_unrevert = session.unrevert_tree

        async def change_before_unrevert(
            *,
            expected_active_leaf_id: str | None,
            operation_id: str | None = None,
            cancel_requested: Callable[[], bool] | None = None,
        ) -> object:
            await session.append_message(Message(role="assistant", content="concurrent"))
            return await original_unrevert(
                expected_active_leaf_id=expected_active_leaf_id,
                operation_id=operation_id,
                cancel_requested=cancel_requested,
            )

        monkeypatch.setattr(session, "unrevert_tree", change_before_unrevert)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await dispatch_parsed(
                executor,
                {"id": "unrevert", "type": "unrevert_session_tree"},
            )
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is False
        assert fixture.session_state.history == previous_history
        assert fixture.session_state.entry_count == 5
        assert session.read_active_leaf_id() != previous_leaf_id
        assert not any(isinstance(event, RpcSessionTreeUnreverted) for event in fixture.events)
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.error is not None
        assert finished.error.startswith("Session tree changed: expected active leaf")

    anyio.run(scenario)


def test_executor_unrevert_cancellation_before_commit_preserves_leaf(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        session = fixture.sessions.create()
        await session.append_message(Message(role="user", content="first"))
        answer = await session.append_message(Message(role="assistant", content="answer"))
        selected = await session.append_message(Message(role="user", content="second"))
        leaf = await session.append_message(Message(role="assistant", content="old answer"))
        await session.navigate_tree(selected.id, expected_active_leaf_id=leaf.id)
        fixture.session_state.session = session
        fixture.session_state.history = session.read_context_messages()
        fixture.session_state.entry_count = 5
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            async with session._append_lock:
                result = await dispatch_parsed(
                    executor,
                    {"id": "unrevert", "type": "unrevert_session_tree"},
                )
                assert result.running_command is not None
                while session._append_lock.statistics().tasks_waiting == 0:
                    await anyio.sleep(0)
                result.running_command.cancel_scope.cancel()
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is False
        assert session.read_active_leaf_id() == answer.id
        assert fixture.session_state.entry_count == 5
        assert not any(isinstance(event, RpcSessionTreeUnreverted) for event in fixture.events)
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.error == "RPC unrevert_session_tree command cancelled"

    anyio.run(scenario)


def test_executor_unrevert_cancellation_after_commit_reports_success(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        session = fixture.sessions.create()
        await session.append_message(Message(role="user", content="first"))
        await session.append_message(Message(role="assistant", content="answer"))
        selected = await session.append_message(Message(role="user", content="second"))
        leaf = await session.append_message(Message(role="assistant", content="old answer"))
        original_history = session.read_context_messages()
        await session.navigate_tree(selected.id, expected_active_leaf_id=leaf.id)
        fixture.session_state.session = session
        fixture.session_state.history = session.read_context_messages()
        fixture.session_state.entry_count = 5
        original_selected_state = rpc_session_mutation_module.rpc_selected_session_state
        committed = threading.Event()
        release = threading.Event()

        def pause_after_unrevert(selected_session: JsonlSession) -> object:
            committed.set()
            assert release.wait(timeout=5)
            return original_selected_state(selected_session)

        monkeypatch.setattr(
            rpc_session_mutation_module,
            "rpc_selected_session_state",
            pause_after_unrevert,
        )
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await dispatch_parsed(
                executor,
                {"id": "unrevert", "type": "unrevert_session_tree"},
            )
            assert result.running_command is not None
            assert await anyio.to_thread.run_sync(lambda: committed.wait(timeout=5))
            result.running_command.cancel_scope.cancel()
            release.set()
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is True
        assert session.read_active_leaf_id() == leaf.id
        assert fixture.session_state.history == original_history
        assert fixture.session_state.entry_count == 6
        assert any(isinstance(event, RpcSessionTreeUnreverted) for event in fixture.events)
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.ok is True
        assert finished.error is None

    anyio.run(scenario)


def test_executor_unrevert_reports_committed_leaf_after_concurrent_navigation(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        session = fixture.sessions.create()
        await session.append_message(Message(role="user", content="first"))
        answer = await session.append_message(Message(role="assistant", content="answer"))
        selected = await session.append_message(Message(role="user", content="second"))
        leaf = await session.append_message(Message(role="assistant", content="old answer"))
        await session.navigate_tree(selected.id, expected_active_leaf_id=leaf.id)
        fixture.session_state.session = session
        fixture.session_state.history = session.read_context_messages()
        fixture.session_state.entry_count = 5
        original_selected_state = rpc_session_mutation_module.rpc_selected_session_state
        concurrent_session = JsonlSession(session_id=session.session_id, path=session.path)

        async def supersede_unrevert() -> None:
            await concurrent_session.select_active_leaf(
                answer.id,
                expected_active_leaf_id=leaf.id,
            )

        def navigate_after_unrevert(selected_session: JsonlSession) -> object:
            anyio.run(supersede_unrevert)
            return original_selected_state(selected_session)

        monkeypatch.setattr(
            rpc_session_mutation_module,
            "rpc_selected_session_state",
            navigate_after_unrevert,
        )
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await dispatch_parsed(
                executor,
                {"id": "unrevert", "type": "unrevert_session_tree"},
            )
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is True
        assert session.read_active_leaf_id() == answer.id
        assert fixture.session_state.history == session.read_context_messages()
        assert fixture.session_state.entry_count == 7
        event = next(
            event for event in fixture.events if isinstance(event, RpcSessionTreeUnreverted)
        )
        assert event.previous_active_leaf_id == answer.id
        assert event.active_leaf_id == leaf.id

    anyio.run(scenario)


def test_executor_session_tree_reports_validation_and_lookup_failures(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            navigate_without_session = await dispatch_parsed(
                executor,
                {"id": "navigate-none", "type": "navigate_session_tree", "entry_id": "entry"},
            )
            assert navigate_without_session.running_command is None

            no_session_cursor = await dispatch_parsed(
                executor,
                {
                    "id": "no-session-cursor",
                    "type": "get_session_tree",
                    "after_entry_id": "missing",
                },
            )
            no_session_cursor_completed = await receive.receive()
            fixture.coordinator.running_command = no_session_cursor.running_command
            await fixture.coordinator.handle_event(
                no_session_cursor_completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            session = fixture.sessions.create()
            await session.append_message(Message(role="user", content="one"))
            fixture.session_state.session = session
            fixture.session_state.history = session.read_context_messages()
            fixture.session_state.entry_count = 1
            unknown_cursor = await dispatch_parsed(
                executor,
                {
                    "id": "unknown-cursor",
                    "type": "get_session_tree",
                    "after_entry_id": "missing",
                },
            )
            cursor_completed = await receive.receive()
            fixture.coordinator.running_command = unknown_cursor.running_command
            await fixture.coordinator.handle_event(
                cursor_completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            await dispatch_parsed(
                executor,
                {
                    "id": "missing-entry",
                    "type": "navigate_session_tree",
                    "entry_id": "missing",
                },
            )
            entry_completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert no_session_cursor_completed.ok is False
        assert cursor_completed.ok is False
        assert entry_completed.ok is False
        assert fixture.session_state.history == session.read_context_messages()
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [event.error for event in finished] == [
            ("RPC navigate_session_tree command requires an existing persisted session"),
            "Session tree cursor not found: missing",
            "Session tree cursor not found: missing",
            "Session tree entry not found: missing",
        ]

    anyio.run(scenario)


def test_executor_navigation_rejects_concurrent_leaf_change(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        session = fixture.sessions.create()
        selected = await session.append_message(Message(role="user", content="one"))
        await session.append_message(Message(role="assistant", content="answer"))
        previous_history = session.read_context_messages()
        fixture.session_state.session = session
        fixture.session_state.history = previous_history
        fixture.session_state.entry_count = 2
        original_navigate = session.navigate_tree

        async def change_before_navigation(
            entry_id: str,
            *,
            expected_active_leaf_id: str | None,
            operation_id: str | None = None,
            cancel_requested: Callable[[], bool] | None = None,
        ) -> SessionTreeNavigation:
            await session.append_message(Message(role="assistant", content="concurrent"))
            return await original_navigate(
                entry_id,
                expected_active_leaf_id=expected_active_leaf_id,
                operation_id=operation_id,
                cancel_requested=cancel_requested,
            )

        monkeypatch.setattr(session, "navigate_tree", change_before_navigation)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await dispatch_parsed(
                executor,
                {
                    "id": "navigate",
                    "type": "navigate_session_tree",
                    "entry_id": selected.id,
                },
            )
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is False
        assert fixture.session_state.history == previous_history
        assert fixture.session_state.entry_count == 2
        assert not any(isinstance(event, RpcSessionTreeNavigated) for event in fixture.events)
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.error is not None
        assert finished.error.startswith("Session tree changed: expected active leaf")

    anyio.run(scenario)


def test_executor_navigation_cancellation_before_commit_preserves_leaf(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        session = fixture.sessions.create()
        selected = await session.append_message(Message(role="user", content="one"))
        active = await session.append_message(Message(role="assistant", content="answer"))
        fixture.session_state.session = session
        fixture.session_state.history = session.read_context_messages()
        fixture.session_state.entry_count = 2
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            async with session._append_lock:
                result = await dispatch_parsed(
                    executor,
                    {
                        "id": "navigate",
                        "type": "navigate_session_tree",
                        "entry_id": selected.id,
                    },
                )
                assert result.running_command is not None
                while session._append_lock.statistics().tasks_waiting == 0:
                    await anyio.sleep(0)
                result.running_command.cancel_scope.cancel()
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is False
        assert session.read_active_leaf_id() == active.id
        assert fixture.session_state.entry_count == 2
        assert not any(isinstance(event, RpcSessionTreeNavigated) for event in fixture.events)
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.error == "RPC navigate_session_tree command cancelled"

    anyio.run(scenario)


def test_executor_navigation_cancellation_after_commit_reports_success(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        session = fixture.sessions.create()
        selected = await session.append_message(Message(role="user", content="one"))
        await session.append_message(Message(role="assistant", content="answer"))
        fixture.session_state.session = session
        fixture.session_state.history = session.read_context_messages()
        fixture.session_state.entry_count = 2
        original_selected_state = rpc_session_mutation_module.rpc_selected_session_state
        committed = threading.Event()
        release = threading.Event()

        def pause_after_navigation(
            selected_session: JsonlSession,
        ) -> tuple[int, tuple[Message, ...], str | None]:
            committed.set()
            assert release.wait(timeout=5)
            return original_selected_state(selected_session)

        monkeypatch.setattr(
            rpc_session_mutation_module,
            "rpc_selected_session_state",
            pause_after_navigation,
        )
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await dispatch_parsed(
                executor,
                {
                    "id": "navigate",
                    "type": "navigate_session_tree",
                    "entry_id": selected.id,
                },
            )
            assert result.running_command is not None
            assert await anyio.to_thread.run_sync(lambda: committed.wait(timeout=5))
            result.running_command.cancel_scope.cancel()
            release.set()
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is True
        assert session.read_active_leaf_id() is None
        assert fixture.session_state.history == ()
        assert fixture.session_state.entry_count == 3
        report = next(
            event for event in fixture.events if isinstance(event, RpcSessionTreeNavigated)
        )
        assert report.changed is True
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.ok is True
        assert finished.error is None

    anyio.run(scenario)


def test_executor_navigation_cancellation_during_no_op_reports_cancelled(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        session = fixture.sessions.create()
        await session.append_message(Message(role="user", content="one"))
        active = await session.append_message(Message(role="assistant", content="answer"))
        previous_history = session.read_context_messages()
        fixture.session_state.session = session
        fixture.session_state.history = previous_history
        fixture.session_state.entry_count = 2
        original_navigate = session.navigate_tree
        no_op_complete = anyio.Event()
        release = anyio.Event()

        async def pause_after_no_op(
            entry_id: str,
            *,
            expected_active_leaf_id: str | None,
            operation_id: str | None = None,
            cancel_requested: Callable[[], bool] | None = None,
        ) -> SessionTreeNavigation:
            result = await original_navigate(
                entry_id,
                expected_active_leaf_id=expected_active_leaf_id,
                operation_id=operation_id,
                cancel_requested=cancel_requested,
            )
            assert result.changed is False
            no_op_complete.set()
            await release.wait()
            return result

        monkeypatch.setattr(session, "navigate_tree", pause_after_no_op)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await dispatch_parsed(
                executor,
                {
                    "id": "navigate",
                    "type": "navigate_session_tree",
                    "entry_id": active.id,
                },
            )
            assert result.running_command is not None
            await no_op_complete.wait()
            result.running_command.cancel_scope.cancel()
            release.set()
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is False
        assert session.read_active_leaf_id() == active.id
        assert fixture.session_state.history == previous_history
        assert fixture.session_state.entry_count == 2
        assert not any(isinstance(event, RpcSessionTreeNavigated) for event in fixture.events)
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.ok is False
        assert finished.error == "RPC navigate_session_tree command cancelled"

    anyio.run(scenario)
