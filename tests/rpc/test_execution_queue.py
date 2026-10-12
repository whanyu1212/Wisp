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
from wisp.agent.harness import QueuedMessages
from wisp.agent.messages import Message
from wisp.events import (
    ErrorEvent,
    QueueItemsRemoved,
    QueueUpdated,
    RpcCommandFinished,
    RpcCommandStarted,
)
from wisp.rpc.coordinator import _RpcRunningCommand


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)


def test_executor_queue_state_is_idle_safe_and_mutations_fail_cleanly(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            state_result = await dispatch_parsed(
                executor, {"id": "state", "type": "get_queue_state"}, None
            )
            mutation_commands: list[dict[str, object]] = [
                {"id": "steer", "type": "steer", "content": "redirect"},
                {"id": "follow", "type": "follow_up", "content": "continue"},
                {
                    "id": "mode",
                    "type": "set_queue_mode",
                    "kind": "steering",
                    "mode": "all",
                },
                {"id": "pop", "type": "pop_queue", "kind": "steering"},
                {"id": "clear", "type": "clear_queue"},
            ]
            results = [await dispatch_parsed(executor, command) for command in mutation_commands]
            task_group.cancel_scope.cancel()

        assert state_result.running_command is None
        assert all(result.running_command is None for result in results)
        state_event = next(event for event in fixture.events if isinstance(event, QueueUpdated))
        assert state_event.steering == ()
        assert state_event.follow_up == ()
        assert state_event.steering_mode == "one_at_a_time"
        assert state_event.follow_up_mode == "one_at_a_time"
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_id, event.ok, event.error) for event in finished] == [
            ("state", True, None),
            *[
                (command_id, False, "CodingSession has no active agent run")
                for command_id in ("steer", "follow", "mode", "pop", "clear")
            ],
        ]

    anyio.run(scenario)


@pytest.mark.parametrize("id_fields", [{}, {"id": None}, {"id": "queue-1"}])
@pytest.mark.parametrize("content", ["", " \t", "你好 🌸"])
def test_typed_queue_identity_content_and_order(
    tmp_path: Path, monkeypatch: MonkeyPatch, id_fields: dict[str, object], content: str
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        calls: list[object] = []
        state = QueueUpdated()

        async def enqueue(value: str) -> QueueUpdated:
            calls.append(value)
            return state

        def mode(kind: object, value: object) -> QueueUpdated:
            calls.append((kind, value))
            return state

        def pop(kind: object) -> tuple[Message, QueueUpdated]:
            calls.append(kind)
            return Message(role="user", content=content), state

        def clear(kind: object) -> tuple[QueuedMessages, QueueUpdated]:
            calls.append(kind)
            return QueuedMessages(
                steering=(Message(role="user", content=content),)
                if kind in (None, "steering")
                else (),
                follow_up=(Message(role="user", content="later"),)
                if kind in (None, "follow_up")
                else (),
            ), state

        monkeypatch.setattr(fixture.agent, "steer", enqueue)
        monkeypatch.setattr(fixture.agent, "follow_up", enqueue)
        monkeypatch.setattr(fixture.agent, "set_queue_mode", mode)
        monkeypatch.setattr(fixture.agent, "pop_queue", pop)
        monkeypatch.setattr(fixture.agent, "clear_queue", clear)
        payloads: list[dict[str, object]] = [
            {"type": "get_queue_state"},
            {"type": "steer", "content": content},
            {"type": "follow_up", "content": content},
            *[
                {"type": "set_queue_mode", "kind": kind, "mode": value}
                for kind in ("steering", "follow_up")
                for value in ("all", "one_at_a_time")
            ],
            *[{"type": "pop_queue", "kind": kind} for kind in ("steering", "follow_up")],
            *[
                {"type": "clear_queue", **fields}
                for fields in ({}, {"kind": None}, {"kind": "steering"}, {"kind": "follow_up"})
            ],
        ]
        running = _RpcRunningCommand("active", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            for payload in payloads:
                start = len(fixture.events)
                result = await dispatch_parsed(executor, {**payload, **id_fields}, running)
                assert result.running_command is running
                assert fixture.coordinator.running_command is running
                emitted = fixture.events[start:]
                removal = payload["type"] in {"pop_queue", "clear_queue"}
                assert [type(event) for event in emitted] == [
                    RpcCommandStarted,
                    *([QueueItemsRemoved] if removal else []),
                    QueueUpdated,
                    RpcCommandFinished,
                ]
                first, last = emitted[0], emitted[-1]
                assert isinstance(first, RpcCommandStarted)
                assert isinstance(last, RpcCommandFinished)
                assert first.command_id == last.command_id
                assert first.command_id
                assert first.command_type == last.command_type == payload["type"]
                assert last.ok is True
                if id_fields.get("id") is not None:
                    assert first.command_id == id_fields["id"]
                if removal:
                    removed = emitted[1]
                    assert isinstance(removed, QueueItemsRemoved)
                    assert removed.command_id == first.command_id
                    assert removed.kind == payload.get("kind")
                    if payload["type"] == "pop_queue":
                        assert (removed.steering, removed.follow_up) == (
                            ((content,), ()) if payload["kind"] == "steering" else ((), (content,))
                        )
                    else:
                        assert removed.steering == (
                            (content,) if payload.get("kind") in (None, "steering") else ()
                        )
                        assert removed.follow_up == (
                            ("later",) if payload.get("kind") in (None, "follow_up") else ()
                        )
            task_group.cancel_scope.cancel()
        assert calls == [
            content,
            content,
            ("steering", "all"),
            ("steering", "one_at_a_time"),
            ("follow_up", "all"),
            ("follow_up", "one_at_a_time"),
            "steering",
            "follow_up",
            None,
            None,
            "steering",
            "follow_up",
        ]

    anyio.run(scenario)


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
@pytest.mark.parametrize("command_type", ["steer", "follow_up"])
def test_typed_queue_runtime_failure_and_cancellation(
    tmp_path: Path, monkeypatch: MonkeyPatch, error_type: type[Exception], command_type: str
) -> None:
    async def fail(_content: str) -> QueueUpdated:
        raise error_type("queue failed")

    async def blocked(_content: str) -> QueueUpdated:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        monkeypatch.setattr(fixture.agent, command_type, fail)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            await dispatch_parsed(
                executor, {"type": command_type, "id": "failed", "content": "text"}
            )
            assert [type(event) for event in fixture.events] == [
                RpcCommandStarted,
                ErrorEvent,
                RpcCommandFinished,
            ]
            last = fixture.events[-1]
            assert isinstance(last, RpcCommandFinished)
            assert last.ok is False and last.error == "queue failed"
            monkeypatch.setattr(fixture.agent, command_type, blocked)
            with anyio.CancelScope() as scope:
                scope.cancel()
                with pytest.raises(anyio.get_cancelled_exc_class()):
                    await dispatch_parsed(
                        executor, {"type": command_type, "id": "cancelled", "content": "text"}
                    )
            assert len(fixture.events) == 4
            assert isinstance(fixture.events[-1], RpcCommandStarted)
            task_group.cancel_scope.cancel()

    anyio.run(scenario)


def test_executor_queue_commands_delegate_and_report_removed_items(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected_session = fixture.sessions.create()
        fixture.session_state.session = selected_session
        calls: list[tuple[object, ...]] = []

        def state(session: object = None) -> QueueUpdated:
            calls.append(("state", session))
            return QueueUpdated(steering=("one", "two"), follow_up=("later",))

        async def steer(content: str) -> QueueUpdated:
            calls.append(("steer", content))
            return QueueUpdated(steering=(content,))

        async def follow_up(content: str) -> QueueUpdated:
            calls.append(("follow_up", content))
            return QueueUpdated(follow_up=(content,))

        def set_mode(kind: object, mode: object) -> QueueUpdated:
            calls.append(("set_queue_mode", kind, mode))
            return QueueUpdated(steering_mode="all")

        def pop(kind: object) -> tuple[Message, QueueUpdated]:
            calls.append(("pop_queue", kind))
            return Message(role="user", content="two"), QueueUpdated(steering=("one",))

        def clear(kind: object = None) -> tuple[QueuedMessages, QueueUpdated]:
            calls.append(("clear_queue", kind))
            return (
                QueuedMessages(follow_up=(Message(role="user", content="later"),)),
                QueueUpdated(),
            )

        monkeypatch.setattr(fixture.agent, "queue_state", state)
        monkeypatch.setattr(fixture.agent, "steer", steer)
        monkeypatch.setattr(fixture.agent, "follow_up", follow_up)
        monkeypatch.setattr(fixture.agent, "set_queue_mode", set_mode)
        monkeypatch.setattr(fixture.agent, "pop_queue", pop)
        monkeypatch.setattr(fixture.agent, "clear_queue", clear)

        commands: list[dict[str, object]] = [
            {"id": "state", "type": "get_queue_state"},
            {"id": "steer", "type": "steer", "content": "redirect"},
            {"id": "follow", "type": "follow_up", "content": "continue"},
            {
                "id": "mode",
                "type": "set_queue_mode",
                "kind": "steering",
                "mode": "all",
            },
            {"id": "pop", "type": "pop_queue", "kind": "steering"},
            {"id": "clear", "type": "clear_queue", "kind": "follow_up"},
        ]
        running = _RpcRunningCommand("prompt", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            results = [await dispatch_parsed(executor, command, running) for command in commands]
            task_group.cancel_scope.cancel()

        assert all(result.running_command is running for result in results)
        assert calls == [
            ("state", selected_session),
            ("steer", "redirect"),
            ("follow_up", "continue"),
            ("set_queue_mode", "steering", "all"),
            ("pop_queue", "steering"),
            ("clear_queue", "follow_up"),
        ]
        removed = [event for event in fixture.events if isinstance(event, QueueItemsRemoved)]
        assert [
            (
                event.command_id,
                event.operation,
                event.kind,
                event.steering,
                event.follow_up,
            )
            for event in removed
        ] == [
            ("pop", "pop", "steering", ("two",), ()),
            ("clear", "clear", "follow_up", (), ("later",)),
        ]
        assert all(event.ok for event in fixture.events if isinstance(event, RpcCommandFinished))

    anyio.run(scenario)


def test_executor_reports_empty_pop_and_clear_as_success(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        monkeypatch.setattr(
            fixture.agent,
            "pop_queue",
            lambda _kind: (None, QueueUpdated()),
        )
        monkeypatch.setattr(
            fixture.agent,
            "clear_queue",
            lambda _kind=None: (QueuedMessages(), QueueUpdated()),
        )
        running = _RpcRunningCommand("prompt", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            await dispatch_parsed(
                executor, {"id": "pop", "type": "pop_queue", "kind": "steering"}, running
            )
            await dispatch_parsed(executor, {"id": "clear", "type": "clear_queue"}, running)
            task_group.cancel_scope.cancel()

        removed = [event for event in fixture.events if isinstance(event, QueueItemsRemoved)]
        assert [
            (event.command_id, event.kind, event.steering, event.follow_up) for event in removed
        ] == [
            ("pop", "steering", (), ()),
            ("clear", None, (), ()),
        ]
        assert [
            (event.command_id, event.ok)
            for event in fixture.events
            if isinstance(event, RpcCommandFinished)
        ] == [("pop", True), ("clear", True)]

    anyio.run(scenario)
