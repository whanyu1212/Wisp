from __future__ import annotations

import shutil
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
from wisp.agent.harness import QueuedMessages
from wisp.agent.messages import Message
from wisp.coding.session import _RetainedQueueState
from wisp.events import (
    RpcCommandFinished,
    RpcCommandStarted,
    RpcSessionCloned,
    RpcSessionForked,
    RpcSessionNameChanged,
    RpcSessionSelected,
    RpcSessionsReported,
    RpcStateReported,
)
from wisp.rpc import execution as rpc_execution_module
from wisp.rpc.coordinator import _RpcCommandCompleted
from wisp.rpc.session.state import rpc_selected_session_state
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)


def test_rpc_session_state_reads_one_coherent_run_snapshot(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> None:
        await session.append_message(Message(role="user", content="one"))
        await session.append_message(Message(role="assistant", content="two"))
        await session.set_name("Named session")

    anyio.run(write)
    original_read_run_snapshot = session.read_run_snapshot
    read_count = 0

    def count_reads() -> object:
        nonlocal read_count
        read_count += 1
        return original_read_run_snapshot()

    monkeypatch.setattr(session, "read_run_snapshot", count_reads)
    monkeypatch.setattr(
        session,
        "read_context_messages",
        lambda: pytest.fail("startup replayed context separately"),
    )
    monkeypatch.setattr(
        session,
        "read_name",
        lambda: pytest.fail("startup scanned the session name separately"),
    )

    state = rpc_execution_module.rpc_session_state(session)

    assert read_count == 1
    assert state.entry_count == 3
    assert [message.content for message in state.history] == ["one", "two"]
    assert state.name == "Named session"


def test_updated_rpc_session_state_reads_entries_once(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> None:
        await session.append_message(Message(role="user", content="one"))
        await session.append_message(Message(role="assistant", content="two"))

    anyio.run(write)
    original_read_entry_snapshot = session.read_entry_snapshot
    read_count = 0

    def count_reads() -> tuple[object, ...]:
        nonlocal read_count
        read_count += 1
        return original_read_entry_snapshot()

    monkeypatch.setattr(session, "read_entry_snapshot", count_reads)

    entry_count, history = rpc_execution_module.updated_rpc_session_state(session, (), 0)

    assert entry_count == 2
    assert [message.content for message in history] == ["one", "two"]
    assert read_count == 1


def test_executor_sessions_reports_empty_catalog(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await dispatch_parsed(executor, {"id": "sessions", "type": "get_sessions"})
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert result.running_command.command_type == "get_sessions"
        assert completed.command_id == "sessions"
        assert completed.command_type == "get_sessions"
        assert completed.ok is True
        assert completed.history is None
        assert completed.entry_count == 0
        assert completed.selected_session is None
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcSessionsReported,
            RpcCommandFinished,
        ]
        report = fixture.events[1]
        assert isinstance(report, RpcSessionsReported)
        assert report.sessions == ()
        assert report.selected_session_id is None
        assert report.selected_session_path is None

    anyio.run(scenario)


def test_executor_sessions_cancel_abandons_blocked_catalog_read(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        started = threading.Event()
        release = threading.Event()
        original_catalog = fixture.sessions.catalog_page

        def blocked_catalog(
            *, limit: int, query: str, cursor: str | None, check_cancelled: Callable[[], None]
        ) -> object:
            started.set()
            release.wait(timeout=5)
            return original_catalog(
                limit=limit, query=query, cursor=cursor, check_cancelled=check_cancelled
            )

        monkeypatch.setattr(fixture.sessions, "catalog_page", blocked_catalog)

        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await dispatch_parsed(executor, {"id": "sessions", "type": "get_sessions"})
            assert result.running_command is not None

            try:
                with anyio.fail_after(1):
                    while not started.is_set():
                        await anyio.sleep(0.01)

                result.running_command.cancel_scope.cancel()

                with anyio.fail_after(1):
                    completed = await receive.receive()
            finally:
                release.set()
                task_group.cancel_scope.cancel()

        assert completed.command_id == "sessions"
        assert completed.command_type == "get_sessions"
        assert completed.ok is False
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcCommandFinished,
        ]
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.error == "RPC get_sessions command cancelled"

    anyio.run(scenario)


def test_executor_sessions_reports_bounded_catalog_with_selected_metadata(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        first = fixture.sessions.create()
        await first.append_message(Message(role="user", content="one"))
        second = fixture.sessions.create()
        await second.append_message(Message(role="user", content="two"))
        fixture.session_state.session = first
        fixture.session_state.entry_count = 1
        event_loop_thread = threading.get_ident()

        def read_name_from_worker() -> str:
            assert threading.get_ident() != event_loop_thread
            return "Selected"

        monkeypatch.setattr(first, "read_name", read_name_from_worker)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await dispatch_parsed(
                executor,
                {"id": "sessions", "type": "get_sessions", "limit": 1},
            )
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert result.selected_session is None
        assert completed.ok is True
        assert completed.history is None
        assert completed.entry_count == 1
        report = next(event for event in fixture.events if isinstance(event, RpcSessionsReported))
        assert len(report.sessions) == 1
        assert report.sessions[0].session_id in {first.session_id, second.session_id}
        assert report.sessions[0].session_path in {first.path, second.path}
        assert report.sessions[0].entry_count == 1
        assert report.sessions[0].active_leaf_id is not None
        assert report.selected_session_id == first.session_id
        assert report.selected_session_path == first.path
        assert report.selected_session_name == "Selected"
        assert fixture.session_state.session is first

    anyio.run(scenario)


def test_executor_select_session_updates_coordinator_state(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()
        await selected.append_message(Message(role="user", content="one"))
        await selected.append_message(Message(role="assistant", content="two"))
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await dispatch_parsed(
                executor,
                {"id": "select", "type": "select_session", "session_id": selected.session_id},
            )
            completed = await receive.receive()
            assert not any(isinstance(event, RpcSessionSelected) for event in fixture.events)
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert result.running_command.command_type == "select_session"
        assert completed.ok is True
        assert completed.post_apply_events
        assert completed.selected_session is not None
        assert completed.selected_session.session_id == selected.session_id
        assert completed.selected_session.path == selected.path
        assert [message.content for message in completed.history or ()] == ["one", "two"]
        assert completed.entry_count == 2
        assert fixture.session_state.session is completed.selected_session
        assert [message.content for message in fixture.session_state.history] == ["one", "two"]
        assert fixture.session_state.entry_count == 2
        report = next(event for event in fixture.events if isinstance(event, RpcSessionSelected))
        assert report.command_id == "select"
        assert report.session_id == selected.session_id
        assert report.session_path == selected.path
        assert report.active_leaf_id is not None
        assert report.entry_count == 2

    anyio.run(scenario)


def test_executor_set_session_name_updates_selected_cached_state(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()
        await selected.append_message(Message(role="user", content="one"))
        fixture.session_state.session = selected
        fixture.session_state.history = (Message(role="user", content="one"),)
        fixture.session_state.entry_count = 1
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await dispatch_parsed(
                executor,
                {
                    "id": "name",
                    "type": "set_session_name",
                    "name": "  Alpha\r\nBeta  ",
                    "session_id": None,
                },
            )
            completed = await receive.receive()
            assert not any(isinstance(event, RpcSessionNameChanged) for event in fixture.events)
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            await dispatch_parsed(executor, {"id": "state", "type": "get_state"}, None)
            task_group.cancel_scope.cancel()

        assert completed.ok is True
        assert completed.session_name == "Alpha Beta"
        assert completed.session_name_updated is True
        assert fixture.session_state.session is selected
        assert fixture.session_state.name == "Alpha Beta"
        assert fixture.session_state.entry_count == 2
        name_events = [
            event for event in fixture.events if isinstance(event, RpcSessionNameChanged)
        ]
        changed = name_events[0]
        assert changed.command_id == "name"
        assert changed.session_id == selected.session_id
        assert changed.previous_name is None
        assert changed.name == "Alpha Beta"
        assert changed.entry_count == 2
        state = next(event for event in fixture.events if isinstance(event, RpcStateReported))
        assert state.state.session_name == "Alpha Beta"

    anyio.run(scenario)


def test_executor_set_session_name_with_explicit_id_does_not_switch_selection(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()
        await selected.append_message(Message(role="user", content="selected"))
        await selected.set_name("Selected")
        other = fixture.sessions.create()
        await other.append_message(Message(role="user", content="other"))
        fixture.session_state.session = selected
        fixture.session_state.history = (Message(role="user", content="selected"),)
        fixture.session_state.entry_count = 2
        fixture.session_state.name = "Selected"
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await dispatch_parsed(
                executor,
                {
                    "id": "name",
                    "type": "set_session_name",
                    "session_id": other.session_id,
                    "name": "Other",
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

        assert completed.ok is True
        assert completed.session_name_updated is False
        assert fixture.session_state.session is selected
        assert fixture.session_state.name == "Selected"
        assert fixture.session_state.entry_count == 2
        assert other.read_name() == "Other"
        name_events = [
            event for event in fixture.events if isinstance(event, RpcSessionNameChanged)
        ]
        changed = name_events[0]
        assert changed.session_id == other.session_id
        assert changed.name == "Other"

    anyio.run(scenario)


def test_executor_set_session_name_with_explicit_path_requires_same_selected_path(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()
        await selected.append_message(Message(role="user", content="selected"))
        await selected.set_name("Selected")
        copied_path = tmp_path / "copied-session.jsonl"
        shutil.copy2(selected.path, copied_path)
        copied = fixture.sessions.load(copied_path)
        assert copied.session_id == selected.session_id
        fixture.session_state.session = selected
        fixture.session_state.history = selected.read_context_messages()
        fixture.session_state.entry_count = len(selected.read_entries())
        fixture.session_state.name = "Selected"
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await dispatch_parsed(
                executor,
                {
                    "id": "name",
                    "type": "set_session_name",
                    "session_id": str(copied_path),
                    "name": "Copied",
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

        assert completed.ok is True
        assert completed.session_name_updated is False
        assert fixture.session_state.session is selected
        assert fixture.session_state.history == selected.read_context_messages()
        assert fixture.session_state.entry_count == 2
        assert fixture.session_state.name == "Selected"
        assert selected.read_name() == "Selected"
        assert copied.read_name() == "Copied"

    anyio.run(scenario)


def test_executor_set_session_name_matches_selected_session_by_normalized_path(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.chdir(tmp_path)
        relative_store = JsonlSessionStore(Path("sessions"))
        fixture = await build_rpc_executor_fixture(tmp_path / "unused")
        fixture.sessions = relative_store
        selected = relative_store.create()
        await selected.append_message(Message(role="user", content="selected"))
        await selected.set_name("Selected")
        fixture.session_state.session = selected
        fixture.session_state.history = selected.read_context_messages()
        fixture.session_state.entry_count = len(selected.read_entries())
        fixture.session_state.name = "Selected"
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await dispatch_parsed(
                executor,
                {
                    "id": "name",
                    "type": "set_session_name",
                    "session_id": str(selected.path.resolve(strict=False)),
                    "name": "Renamed",
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

        assert completed.ok is True
        assert completed.session_name_updated is True
        assert fixture.session_state.session is selected
        assert fixture.session_state.entry_count == 3
        assert fixture.session_state.name == "Renamed"
        assert selected.read_name() == "Renamed"

    anyio.run(scenario)


def test_rpc_selected_session_state_reads_name_from_the_same_entry_snapshot(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> None:
        await session.append_message(Message(role="user", content="selected"))
        await session.set_name("Selected")

    anyio.run(write)

    def fail_second_name_read() -> str | None:
        raise AssertionError("name should come from the already-read entries")

    monkeypatch.setattr(session, "read_name", fail_second_name_read)

    entry_count, history, active_leaf_id, name = rpc_selected_session_state(session)

    assert entry_count == 2
    assert [message.content for message in history] == ["selected"]
    assert active_leaf_id is not None
    assert name == "Selected"


def test_executor_select_session_reports_validation_and_load_failures(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        previous = fixture.sessions.create()
        await previous.append_message(Message(role="user", content="previous"))
        fixture.session_state.session = previous
        fixture.session_state.history = (Message(role="user", content="previous"),)
        fixture.session_state.entry_count = 1
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            missing = await dispatch_parsed(
                executor,
                {"id": "missing", "type": "select_session", "session_id": "missing"},
            )
            completed = await receive.receive()
            fixture.coordinator.running_command = missing.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert missing.running_command is not None
        assert completed.ok is False
        assert completed.selected_session is None
        assert completed.history is None
        assert completed.entry_count == 1
        assert fixture.session_state.session is previous
        assert [message.content for message in fixture.session_state.history] == ["previous"]
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_type, event.ok, event.error) for event in finished] == [
            ("select_session", False, "Session not found: missing"),
        ]
        assert not any(isinstance(event, RpcSessionSelected) for event in fixture.events)

    anyio.run(scenario)


def test_executor_clone_session_applies_target_before_reporting_success(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        source = fixture.sessions.create()
        await source.append_message(Message(role="user", content="one"))
        await source.append_message(Message(role="assistant", content="two"))
        source_before = source.path.read_bytes()
        fixture.session_state.session = source
        fixture.session_state.history = source.read_context_messages()
        fixture.session_state.entry_count = len(source.read_entries())
        fixture.agent._retained_queues[source.session_id] = _RetainedQueueState(  # noqa: SLF001
            messages=QueuedMessages(
                steering=(Message(role="user", content="source steering"),),
                follow_up=(Message(role="user", content="source follow-up"),),
            ),
            steering_mode="all",
            follow_up_mode="all",
        )
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await dispatch_parsed(executor, {"id": "clone", "type": "clone_session"})
            completed = await receive.receive()
            assert not any(isinstance(event, RpcSessionCloned) for event in fixture.events)
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert result.running_command.command_type == "clone_session"
        assert completed.ok is True
        assert completed.selected_session is not None
        assert completed.selected_session is fixture.session_state.session
        assert completed.selected_session.session_id != source.session_id
        assert completed.selected_session.read_context_messages() == source.read_context_messages()
        assert fixture.session_state.history == source.read_context_messages()
        assert fixture.session_state.entry_count == 2
        assert source.path.read_bytes() == source_before
        assert fixture.agent.queue_state(source).steering == ("source steering",)
        assert fixture.agent.queue_state(source).follow_up == ("source follow-up",)
        assert fixture.agent.queue_state(completed.selected_session).steering == ()
        assert fixture.agent.queue_state(completed.selected_session).follow_up == ()
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcSessionCloned,
            RpcCommandFinished,
        ]
        report = fixture.events[1]
        assert isinstance(report, RpcSessionCloned)
        assert report.source_session_id == source.session_id
        assert report.source_session_path == source.path
        assert report.session_id == completed.selected_session.session_id
        assert report.session_path == completed.selected_session.path
        assert report.active_leaf_id == completed.selected_session.read_active_leaf_id()
        assert report.entry_count == 2

    anyio.run(scenario)


def test_executor_clone_session_reports_name_from_inherited_clone_snapshot(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        source = fixture.sessions.create()
        await source.append_message(Message(role="user", content="one"))
        await source.set_name("Named Source")
        fixture.session_state.session = source
        fixture.session_state.history = source.read_context_messages()
        fixture.session_state.entry_count = len(source.read_entries())

        def stale_source_name_read() -> str | None:
            raise AssertionError("clone event must not pre-read the source name")

        monkeypatch.setattr(source, "read_name", stale_source_name_read)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await dispatch_parsed(executor, {"id": "clone", "type": "clone_session"})
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is True
        assert completed.selected_session is fixture.session_state.session
        assert completed.selected_session is not None
        assert fixture.session_state.name == "Named Source"
        report = next(event for event in fixture.events if isinstance(event, RpcSessionCloned))
        assert report.source_session_name == "Named Source"
        assert report.session_name == "Named Source"
        assert report.entry_count == 2
        assert completed.selected_session.read_name() == "Named Source"

    anyio.run(scenario)


def test_executor_fork_session_returns_prompt_and_selects_parent_path(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        source = fixture.sessions.create()
        await source.append_message(Message(role="user", content="first"))
        answer = await source.append_message(Message(role="assistant", content="answer"))
        editable_prompt = "edit me 漢🙂" * 2_000
        selected = await source.append_message(Message(role="user", content=editable_prompt))
        abandoned = await source.append_message(Message(role="assistant", content="old answer"))
        await source.select_active_leaf(
            answer.id,
            expected_active_leaf_id=abandoned.id,
        )
        await source.append_message(Message(role="user", content="active alternative"))
        await source.append_message(Message(role="assistant", content="new answer"))
        fixture.session_state.session = source
        fixture.session_state.history = source.read_context_messages()
        fixture.session_state.entry_count = len(source.read_entries())
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await dispatch_parsed(
                executor,
                {"id": "fork", "type": "fork_session", "entry_id": selected.id},
            )
            completed = await receive.receive()
            assert not any(isinstance(event, RpcSessionForked) for event in fixture.events)
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is True
        assert completed.selected_session is fixture.session_state.session
        assert completed.selected_session is not None
        assert [
            message.content for message in completed.selected_session.read_context_messages()
        ] == ["first", "answer"]
        assert [message.content for message in fixture.session_state.history] == [
            "first",
            "answer",
        ]
        report = next(event for event in fixture.events if isinstance(event, RpcSessionForked))
        assert report.source_session_id == source.session_id
        assert report.source_active_leaf_id == source.read_active_leaf_id()
        assert report.selected_entry_id == selected.id
        assert report.selected_prompt == editable_prompt
        assert report.session_id == completed.selected_session.session_id
        assert report.active_leaf_id == completed.selected_session.read_active_leaf_id()

    anyio.run(scenario)


def test_executor_fork_session_reports_source_name_from_branch_snapshot(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        source = fixture.sessions.create()
        await source.set_name("Named Source")
        await source.append_message(Message(role="user", content="first"))
        answer = await source.append_message(Message(role="assistant", content="answer"))
        selected = await source.append_message(Message(role="user", content="edit me"))
        fixture.session_state.session = source
        fixture.session_state.history = source.read_context_messages()
        fixture.session_state.entry_count = len(source.read_entries())

        def stale_source_name_read() -> str | None:
            raise AssertionError("fork event must not pre-read the source name")

        monkeypatch.setattr(source, "read_name", stale_source_name_read)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await dispatch_parsed(
                executor,
                {"id": "fork", "type": "fork_session", "entry_id": selected.id},
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
        assert completed.selected_session is fixture.session_state.session
        assert completed.selected_session is not None
        assert completed.selected_session.read_name() is None
        assert fixture.session_state.name is None
        assert completed.selected_session.read_active_leaf_id() == answer.id
        report = next(event for event in fixture.events if isinstance(event, RpcSessionForked))
        assert report.source_session_name == "Named Source"
        assert report.session_name is None
        assert report.selected_entry_id == selected.id
        assert report.selected_prompt == "edit me"

    anyio.run(scenario)


def test_executor_first_message_fork_selects_reserved_empty_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        source = fixture.sessions.create()
        selected = await source.append_message(Message(role="user", content="edit first"))
        await source.append_message(Message(role="assistant", content="old answer"))
        fixture.session_state.session = source
        fixture.session_state.history = source.read_context_messages()
        fixture.session_state.entry_count = len(source.read_entries())
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await dispatch_parsed(
                executor,
                {"id": "fork", "type": "fork_session", "entry_id": selected.id},
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
        assert completed.selected_session is fixture.session_state.session
        assert completed.selected_session is not None
        assert not completed.selected_session.path.exists()
        assert fixture.session_state.history == ()
        assert fixture.session_state.entry_count == 0
        report = next(event for event in fixture.events if isinstance(event, RpcSessionForked))
        assert report.active_leaf_id is None
        assert report.entry_count == 0
        assert report.selected_prompt == "edit first"

    anyio.run(scenario)


def test_executor_fork_rejects_a_reserved_empty_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        reserved = fixture.sessions.create()
        fixture.session_state.session = reserved
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await dispatch_parsed(
                executor,
                {"id": "fork", "type": "fork_session", "entry_id": "entry"},
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
        assert fixture.session_state.session is reserved
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.error == "Cannot fork an empty session"

    anyio.run(scenario)


def test_executor_session_derivation_reports_validation_failures(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            clone_without_selection = await dispatch_parsed(
                executor,
                {"id": "clone", "type": "clone_session"},
            )
            fork_without_selection = await dispatch_parsed(
                executor,
                {"id": "fork", "type": "fork_session", "entry_id": "entry"},
            )
            assert clone_without_selection.running_command is None
            assert fork_without_selection.running_command is None

            empty = fixture.sessions.create()
            fixture.session_state.session = empty
            clone_empty = await dispatch_parsed(
                executor,
                {"id": "clone-empty", "type": "clone_session"},
            )
            clone_completed = await receive.receive()
            fixture.coordinator.running_command = clone_empty.running_command
            await fixture.coordinator.handle_event(
                clone_completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert clone_completed.ok is False
        assert fixture.session_state.session is empty
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_type, event.ok, event.error) for event in finished] == [
            (
                "clone_session",
                False,
                "RPC clone_session command requires a selected session",
            ),
            (
                "fork_session",
                False,
                "RPC fork_session command requires a selected session",
            ),
            ("clone_session", False, "Cannot clone an empty session"),
        ]
        assert not any(
            isinstance(event, RpcSessionCloned | RpcSessionForked) for event in fixture.events
        )

    anyio.run(scenario)


def test_executor_clone_rejects_concurrent_source_leaf_change(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        source = fixture.sessions.create()
        await source.append_message(Message(role="user", content="before"))
        fixture.session_state.session = source
        fixture.session_state.history = source.read_context_messages()
        fixture.session_state.entry_count = len(source.read_entries())
        original_clone = fixture.sessions.clone

        async def change_source_before_clone(
            selected: JsonlSession,
            *,
            expected_active_leaf_id: str | None,
        ) -> JsonlSession:
            assert selected is source
            await source.append_message(Message(role="assistant", content="concurrent"))
            return await original_clone(
                source,
                expected_active_leaf_id=expected_active_leaf_id,
            )

        monkeypatch.setattr(fixture.sessions, "clone", change_source_before_clone)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await dispatch_parsed(executor, {"id": "clone", "type": "clone_session"})
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is False
        assert completed.selected_session is None
        assert fixture.session_state.session is source
        assert [message.content for message in fixture.session_state.history] == ["before"]
        assert tuple(tmp_path.glob("*.jsonl")) == (source.path,)
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.error is not None
        assert finished.error.startswith("Session tree changed: expected active leaf")

    anyio.run(scenario)


def test_executor_fork_rejects_non_user_and_missing_entries(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        source = fixture.sessions.create()
        await source.append_message(Message(role="user", content="one"))
        assistant = await source.append_message(Message(role="assistant", content="two"))
        fixture.session_state.session = source
        fixture.session_state.history = source.read_context_messages()
        fixture.session_state.entry_count = 2
        send, receive = anyio.create_memory_object_stream(10)
        completions: list[_RpcCommandCompleted] = []
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            for index, entry_id in enumerate((assistant.id, "missing"), start=1):
                result = await dispatch_parsed(
                    executor,
                    {
                        "id": f"fork-{index}",
                        "type": "fork_session",
                        "entry_id": entry_id,
                    },
                )
                completed = await receive.receive()
                completions.append(completed)
                fixture.coordinator.running_command = result.running_command
                await fixture.coordinator.handle_event(
                    completed,
                    dispatch=preserve_running_command,
                    reject=reject_unexpected_command,
                )
            task_group.cancel_scope.cancel()

        assert all(not completion.ok for completion in completions)
        assert fixture.session_state.session is source
        assert tuple(tmp_path.glob("*.jsonl")) == (source.path,)
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [event.error for event in finished] == [
            f"Session fork entry must be a persisted user message: {assistant.id}",
            "Session fork entry must be a persisted user message: missing",
        ]
        assert not any(isinstance(event, RpcSessionForked) for event in fixture.events)

    anyio.run(scenario)


def test_executor_clone_cancellation_before_commit_preserves_source(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        source = fixture.sessions.create()
        await source.append_message(Message(role="user", content="before"))
        fixture.session_state.session = source
        fixture.session_state.history = source.read_context_messages()
        fixture.session_state.entry_count = 1
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await dispatch_parsed(executor, {"id": "clone", "type": "clone_session"})
            assert result.running_command is not None
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
        assert completed.selected_session is None
        assert fixture.session_state.session is source
        assert tuple(tmp_path.glob("*.jsonl")) == (source.path,)
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.error == "RPC clone_session command cancelled"

    anyio.run(scenario)


def test_executor_clone_cancellation_after_commit_reports_success(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        source = fixture.sessions.create()
        await source.append_message(Message(role="user", content="before"))
        fixture.session_state.session = source
        fixture.session_state.history = source.read_context_messages()
        fixture.session_state.entry_count = 1
        original_clone = fixture.sessions.clone
        published = anyio.Event()
        release = anyio.Event()

        async def pause_after_clone(
            selected: JsonlSession,
            *,
            expected_active_leaf_id: str | None,
        ) -> JsonlSession:
            cloned = await original_clone(
                selected,
                expected_active_leaf_id=expected_active_leaf_id,
            )
            published.set()
            await release.wait()
            return cloned

        monkeypatch.setattr(fixture.sessions, "clone", pause_after_clone)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await dispatch_parsed(executor, {"id": "clone", "type": "clone_session"})
            assert result.running_command is not None
            await published.wait()
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
        assert completed.selected_session is fixture.session_state.session
        assert completed.selected_session is not None
        assert completed.selected_session.session_id != source.session_id
        assert len(tuple(tmp_path.glob("*.jsonl"))) == 2
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.ok is True
        assert finished.error is None

    anyio.run(scenario)
