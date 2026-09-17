from __future__ import annotations

from wisp.events import ErrorEvent, RpcCommandFinished, RpcCommandStarted, WispEvent
from wisp.rpc.commands import GetStateCommand
from wisp.rpc.lifecycle import _MAX_RPC_COMMAND_ERROR_CHARS, RpcCommandLifecycle


def test_for_command_uses_supplied_identity() -> None:
    events: list[WispEvent] = []
    command = GetStateCommand(id="state-1")
    lifecycle = RpcCommandLifecycle.for_command(command, write_event=events.append)

    assert lifecycle.command_id == "state-1"
    assert lifecycle.command_type == "get_state"
    assert [type(event) for event in events] == [RpcCommandStarted]
    started = events[0]
    assert isinstance(started, RpcCommandStarted)
    assert started.command_id == "state-1"
    assert started.command_type == "get_state"


def test_for_command_generates_id_when_omitted() -> None:
    events: list[WispEvent] = []
    lifecycle = RpcCommandLifecycle.for_command(GetStateCommand(), write_event=events.append)

    assert lifecycle.command_id
    assert len(lifecycle.command_id) == 32
    assert [type(event) for event in events] == [RpcCommandStarted]
    started = events[0]
    assert isinstance(started, RpcCommandStarted)
    assert started.command_id == lifecycle.command_id
    assert started.command_type == "get_state"


def test_start_generates_id_for_unknown_rejection() -> None:
    events: list[WispEvent] = []
    lifecycle = RpcCommandLifecycle.start(
        command_id=None,
        command_type="future_command",
        write_event=events.append,
    )

    assert lifecycle.command_id
    assert len(lifecycle.command_id) == 32
    started = events[0]
    assert isinstance(started, RpcCommandStarted)
    assert started.command_type == "future_command"


def test_fail_emits_error_then_finished_and_bounds_message() -> None:
    events: list[WispEvent] = []
    lifecycle = RpcCommandLifecycle.for_command(
        GetStateCommand(id="state-1"),
        write_event=events.append,
    )
    oversized = "x" * (_MAX_RPC_COMMAND_ERROR_CHARS + 1)
    lifecycle.fail(oversized)

    assert [type(event) for event in events] == [
        RpcCommandStarted,
        ErrorEvent,
        RpcCommandFinished,
    ]
    error, finished = events[1], events[2]
    assert isinstance(error, ErrorEvent)
    assert isinstance(finished, RpcCommandFinished)
    assert len(error.message) == _MAX_RPC_COMMAND_ERROR_CHARS
    assert error.message.endswith("...")
    assert finished.ok is False
    assert finished.error == error.message
    assert finished.command_id == "state-1"
    assert finished.command_type == "get_state"


def test_bind_fail_does_not_emit_started() -> None:
    events: list[WispEvent] = []
    lifecycle = RpcCommandLifecycle.bind(
        command_id="approval-1",
        command_type="approval",
        write_event=events.append,
    )
    lifecycle.fail("No pending tool approval with call_id: call-1")

    assert [type(event) for event in events] == [ErrorEvent, RpcCommandFinished]
    finished = events[1]
    assert isinstance(finished, RpcCommandFinished)
    assert finished.command_id == "approval-1"
    assert finished.ok is False


def test_finish_emits_only_successful_finished() -> None:
    events: list[WispEvent] = []
    lifecycle = RpcCommandLifecycle.bind(
        command_id="state-1",
        command_type="get_state",
        write_event=events.append,
    )
    lifecycle.finish()

    assert [type(event) for event in events] == [RpcCommandFinished]
    finished = events[0]
    assert isinstance(finished, RpcCommandFinished)
    assert finished.ok is True
    assert finished.error is None
    assert finished.command_id == "state-1"


def test_finish_failure_does_not_emit_error_event() -> None:
    events: list[WispEvent] = []
    target = RpcCommandLifecycle.start(
        command_id="queued-1",
        command_type="prompt",
        write_event=events.append,
    )
    target.finish(ok=False, error="RPC command cancelled: queued-1")

    assert [type(event) for event in events] == [RpcCommandStarted, RpcCommandFinished]
    finished = events[1]
    assert isinstance(finished, RpcCommandFinished)
    assert finished.ok is False
    assert finished.error == "RPC command cancelled: queued-1"
