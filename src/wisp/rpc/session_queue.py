"""Session queue command execution for the RPC frontend."""

from __future__ import annotations

from typing import assert_never

from wisp.coding import CodingSession
from wisp.events import QueueItemsRemoved
from wisp.rpc.commands import (
    ClearQueueCommand,
    FollowUpCommand,
    GetQueueStateCommand,
    PopQueueCommand,
    SetQueueModeCommand,
    SteerCommand,
)
from wisp.rpc.lifecycle import RpcCommandLifecycle, RpcEventWriter
from wisp.sessions.jsonl import JsonlSession

type _RpcQueueCommand = (
    SteerCommand
    | FollowUpCommand
    | GetQueueStateCommand
    | SetQueueModeCommand
    | PopQueueCommand
    | ClearQueueCommand
)


async def handle_rpc_queue_command(
    command: _RpcQueueCommand,
    *,
    agent: CodingSession,
    session: JsonlSession | None,
    write_event: RpcEventWriter,
) -> None:
    """Execute one ordered queue command through the shared session facade."""

    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    command_id = lifecycle.command_id

    removed: QueueItemsRemoved | None = None
    try:
        if isinstance(command, GetQueueStateCommand):
            state = agent.queue_state(session)
        elif isinstance(command, SteerCommand):
            state = await agent.steer(command.content)
        elif isinstance(command, FollowUpCommand):
            state = await agent.follow_up(command.content)
        elif isinstance(command, SetQueueModeCommand):
            kind = command.kind
            mode = command.mode
            state = agent.set_queue_mode(kind, mode)
        elif isinstance(command, PopQueueCommand):
            kind = command.kind
            popped, state = agent.pop_queue(kind)
            removed = QueueItemsRemoved(
                command_id=command_id,
                operation="pop",
                kind=kind,
                steering=(popped.user_visible_content,)
                if popped is not None and kind == "steering"
                else (),
                follow_up=(popped.user_visible_content,)
                if popped is not None and kind == "follow_up"
                else (),
            )
        elif isinstance(command, ClearQueueCommand):
            clear_kind = command.kind
            cleared, state = agent.clear_queue(clear_kind)
            removed = QueueItemsRemoved(
                command_id=command_id,
                operation="clear",
                kind=clear_kind,
                steering=tuple(message.user_visible_content for message in cleared.steering),
                follow_up=tuple(message.user_visible_content for message in cleared.follow_up),
            )
        else:  # pragma: no cover - dispatch owns the closed command set
            assert_never(command)
    except (RuntimeError, ValueError) as exc:
        lifecycle.fail(str(exc))
        return

    if removed is not None:
        write_event(removed)
    write_event(state)
    lifecycle.finish()
