from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import anyio
import pytest

from tests.rpc.coordinator_support import (
    Receiver,
    expected_commands,
    ignore_reject,
    input_command,
    parsed_command,
)
from wisp.events import RpcSessionSelected, WispEvent
from wisp.messages import Message
from wisp.rpc.commands import ParsedRpcCommand
from wisp.rpc.coordinator import (
    RpcCoordinator,
    _RpcCommandCompleted,
    _RpcDispatchResult,
    _RpcInputClosed,
    _RpcRunningCommand,
    _RpcSessionState,
)
from wisp.sessions.jsonl import JsonlSessionStore


def test_coordinator_runs_queued_commands_in_fifo_order() -> None:
    async def scenario() -> None:
        history = (Message(role="user", content="done"),)
        receiver = Receiver(
            [
                input_command({"prompt": "", "id": "one", "type": "prompt"}),
                input_command({"prompt": "", "id": "two", "type": "prompt"}),
                input_command({"prompt": "", "id": "three", "type": "prompt"}),
                _RpcCommandCompleted("one", "prompt", True, history, 1),
                _RpcCommandCompleted("two", "prompt", True, history, 2),
                _RpcCommandCompleted("three", "prompt", True, history, 3),
                _RpcInputClosed(),
            ]
        )
        state = _RpcSessionState(session=None, history=(), entry_count=0)
        coordinator = RpcCoordinator(state)
        dispatched: list[str] = []

        async def dispatch(
            command: ParsedRpcCommand,
            _running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command.command_id)
            dispatched.append(command_id)
            return _RpcDispatchResult(_RpcRunningCommand(command_id, "prompt", anyio.CancelScope()))

        should_shutdown = await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=ignore_reject,
        )

        assert should_shutdown is False
        assert dispatched == ["one", "two", "three"]
        assert state.history == history
        assert state.entry_count == 3

    anyio.run(scenario)


def test_coordinator_applies_new_session_reset_atomically(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = JsonlSessionStore(tmp_path).create()
        await session.append_message(Message(role="user", content="previous"))
        history = (Message(role="user", content="previous"),)
        state = _RpcSessionState(
            session=session,
            history=history,
            entry_count=3,
            name="Previous",
        )
        resets: list[object] = []
        coordinator = RpcCoordinator(
            state, session_grants_reset=lambda: resets.append(state.session)
        )
        receiver = Receiver(
            [
                input_command({"id": "new-1", "type": "new_session"}),
                _RpcInputClosed(),
            ]
        )

        async def dispatch(
            _command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            return _RpcDispatchResult(running_command=running, reset_session=True)

        await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=ignore_reject,
        )

        assert resets == [session]
        assert state.session is None
        assert state.history == ()
        assert state.entry_count == 0
        assert state.name is None
        assert session.path.is_file()

    anyio.run(scenario)


@pytest.mark.parametrize("command_type", ["get_session_stats", "get_messages"])
def test_coordinator_queues_new_session_behind_work_already_waiting_on_background_read(
    command_type: Literal["get_session_stats", "get_messages"],
) -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        background = _RpcRunningCommand("background", command_type, anyio.CancelScope())
        coordinator.running_command = background
        coordinator.queued_commands.append(
            parsed_command({"prompt": "", "id": "prompt", "type": "prompt"})
        )
        dispatched: list[str] = []

        await coordinator.handle_event(
            input_command({"id": "new", "type": "new_session"}),
            dispatch=lambda command, running: (
                dispatched.append(str(command.command_id)) or _RpcDispatchResult(running)
            ),
            reject=ignore_reject,
        )

        assert dispatched == []
        assert list(coordinator.queued_commands) == expected_commands(
            [{"prompt": "", "id": "prompt", "type": "prompt"}, {"id": "new", "type": "new_session"}]
        )
        assert background.cancel_scope.cancel_called is False

    anyio.run(scenario)


@pytest.mark.parametrize(
    "command_type",
    ["get_messages", "get_sessions", "get_session_stats", "get_session_tree"],
)
def test_coordinator_queues_new_session_behind_active_ordered_read(
    command_type: Literal["get_messages", "get_sessions", "get_session_stats", "get_session_tree"],
) -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        background = _RpcRunningCommand("background", command_type, anyio.CancelScope())
        coordinator.running_command = background
        dispatched: list[str] = []

        await coordinator.handle_event(
            input_command({"id": "new", "type": "new_session"}),
            dispatch=lambda command, running: (
                dispatched.append(str(command.command_id)) or _RpcDispatchResult(running)
            ),
            reject=ignore_reject,
        )

        assert dispatched == []
        assert list(coordinator.queued_commands) == expected_commands(
            [{"id": "new", "type": "new_session"}]
        )
        assert background.cancel_scope.cancel_called is False

    anyio.run(scenario)


def test_coordinator_queues_new_session_behind_work_waiting_on_stats_async() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        stats = _RpcRunningCommand("stats", "get_session_stats", anyio.CancelScope())
        coordinator.running_command = stats
        coordinator.queued_commands.append(
            parsed_command({"prompt": "", "id": "prompt", "type": "prompt"})
        )
        dispatched: list[str] = []

        async def dispatch(
            command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            dispatched.append(str(command.command_id))
            return _RpcDispatchResult(running)

        async def reject(_command: ParsedRpcCommand, _message: str) -> None:
            raise AssertionError("new_session should be queued, not rejected")

        await coordinator.handle_event(
            input_command({"id": "new", "type": "new_session"}),
            dispatch=dispatch,
            reject=reject,
        )

        assert dispatched == []
        assert list(coordinator.queued_commands) == expected_commands(
            [{"prompt": "", "id": "prompt", "type": "prompt"}, {"id": "new", "type": "new_session"}]
        )
        assert stats.cancel_scope.cancel_called is False

    anyio.run(scenario)


@pytest.mark.parametrize(
    ("active_type", "completed_ok"),
    [
        ("prompt", True),
        ("get_session_stats", True),
        ("prompt", False),
    ],
)
def test_coordinator_orders_configure_between_active_work_and_later_prompt(
    active_type: Literal["prompt", "get_session_stats"],
    completed_ok: bool,
) -> None:
    async def scenario() -> None:
        receiver = Receiver(
            [
                input_command(
                    {
                        "id": "active",
                        "type": active_type,
                        **({"prompt": ""} if active_type == "prompt" else {}),
                    }
                ),
                input_command({"provider": "fake", "id": "configure", "type": "configure"}),
                input_command({"prompt": "", "id": "prompt-after", "type": "prompt"}),
                _RpcCommandCompleted("active", active_type, completed_ok, (), 0),
                _RpcCommandCompleted("prompt-after", "prompt", True, (), 1),
                _RpcInputClosed(),
            ]
        )
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        dispatched: list[str] = []

        async def dispatch(
            command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            command_id = str(command.command_id)
            command_type = command.command_type
            dispatched.append(command_id)
            if command_type == "configure":
                assert running is None
                return _RpcDispatchResult(None)
            return _RpcDispatchResult(
                _RpcRunningCommand(command_id, command_type, anyio.CancelScope())
            )

        async def reject(_command: ParsedRpcCommand, _message: str) -> None:
            raise AssertionError("ordered configure should be queued, not rejected")

        await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=reject,
        )

        assert dispatched == ["active", "configure", "prompt-after"]
        assert not coordinator.queued_commands

    anyio.run(scenario)


def test_coordinator_dispatches_new_session_while_init_is_running() -> None:
    async def scenario() -> None:
        sync_coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        sync_coordinator.running_command = _RpcRunningCommand("init", "init", anyio.CancelScope())
        sync_dispatched: list[str] = []

        async def sync_dispatch(
            command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            sync_dispatched.append(str(command.command_id))
            return _RpcDispatchResult(running)

        await sync_coordinator.handle_event(
            input_command({"id": "new-sync", "type": "new_session"}),
            dispatch=sync_dispatch,
            reject=ignore_reject,
        )
        assert sync_dispatched == ["new-sync"]
        assert not sync_coordinator.queued_commands

        async_coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        async_coordinator.running_command = _RpcRunningCommand("init", "init", anyio.CancelScope())
        async_dispatched: list[str] = []

        async def async_dispatch(
            command: ParsedRpcCommand,
            running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            async_dispatched.append(str(command.command_id))
            return _RpcDispatchResult(running)

        async def reject(_command: ParsedRpcCommand, _message: str) -> None:
            raise AssertionError("command unexpectedly rejected")

        await async_coordinator.handle_event(
            input_command({"id": "new-async", "type": "new_session"}),
            dispatch=async_dispatch,
            reject=reject,
        )
        assert async_dispatched == ["new-async"]
        assert not async_coordinator.queued_commands

    anyio.run(scenario)


def test_coordinator_ignores_stale_completion_and_closes_decisions_once() -> None:
    async def scenario() -> None:
        closed = 0

        def on_closed() -> None:
            nonlocal closed
            closed += 1

        receiver = Receiver(
            [
                input_command({"prompt": "", "id": "active", "type": "prompt"}),
                _RpcCommandCompleted("stale", "prompt", True, (), 99),
                _RpcInputClosed(),
                _RpcInputClosed(),
                _RpcCommandCompleted("active", "prompt", True, (), 1),
            ]
        )
        coordinator = RpcCoordinator(
            _RpcSessionState(None, (), 0),
            input_closed_handlers=(on_closed,),
        )

        async def dispatch(
            command: ParsedRpcCommand,
            _running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            return _RpcDispatchResult(
                _RpcRunningCommand(str(command.command_id), "prompt", anyio.CancelScope())
            )

        await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=ignore_reject,
        )

        assert closed == 1
        assert coordinator.session_state.entry_count == 1

    anyio.run(scenario)


def test_coordinator_awaits_completion_events_before_next_async_dispatch() -> None:
    async def scenario() -> None:
        render_started = anyio.Event()
        release_render = anyio.Event()
        next_dispatched = anyio.Event()
        completion_event = RpcSessionSelected(
            command_id="select",
            session_id="session",
            session_path=Path("session.jsonl"),
            entry_count=1,
        )

        async def render_completion(events: tuple[WispEvent, ...]) -> None:
            assert events == (completion_event,)
            render_started.set()
            await release_render.wait()

        receiver = Receiver(
            [
                _RpcCommandCompleted(
                    "select",
                    "select_session",
                    True,
                    (),
                    1,
                    post_apply_events=(completion_event,),
                ),
                _RpcCommandCompleted("next", "get_sessions", True, (), 1),
                _RpcInputClosed(),
            ]
        )
        coordinator = RpcCoordinator(
            _RpcSessionState(None, (), 0),
            completion_event_renderer=render_completion,
        )
        coordinator.running_command = _RpcRunningCommand(
            "select",
            "select_session",
            anyio.CancelScope(),
        )
        coordinator.queued_commands.append(parsed_command({"id": "next", "type": "get_sessions"}))

        async def dispatch(
            command: ParsedRpcCommand,
            _running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            next_dispatched.set()
            return _RpcDispatchResult(
                _RpcRunningCommand(
                    str(command.command_id),
                    "get_sessions",
                    anyio.CancelScope(),
                )
            )

        async def reject(_command: ParsedRpcCommand, _message: str) -> None:
            return None

        async def run_coordinator() -> None:
            await coordinator.run(
                receiver,
                dispatch=dispatch,
                reject=reject,
            )

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run_coordinator)
            await render_started.wait()
            await anyio.sleep(0)
            assert next_dispatched.is_set() is False
            release_render.set()
            await next_dispatched.wait()

    anyio.run(scenario)


def test_coordinator_accepts_compatibility_event_types() -> None:
    @dataclass(frozen=True)
    class CompatInputClosed:
        pass

    @dataclass(frozen=True)
    class CompatCommandCompleted:
        command_id: str
        command_type: Literal["prompt", "compact", "get_session_stats"]
        ok: bool
        history: tuple[Message, ...] | None
        entry_count: int

    async def scenario() -> None:
        receiver = Receiver(
            [
                input_command({"prompt": "", "id": "active", "type": "prompt"}),
                CompatInputClosed(),
                CompatCommandCompleted("active", "prompt", True, (), 1),
            ]
        )
        coordinator = RpcCoordinator(
            _RpcSessionState(None, (), 0),
            input_closed_type=CompatInputClosed,
            command_completed_type=CompatCommandCompleted,
        )

        async def dispatch(
            command: ParsedRpcCommand,
            _running: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            return _RpcDispatchResult(
                _RpcRunningCommand(str(command.command_id), "prompt", anyio.CancelScope())
            )

        await coordinator.run(
            receiver,
            dispatch=dispatch,
            reject=ignore_reject,
        )

        assert coordinator.session_state.entry_count == 1

    anyio.run(scenario)
