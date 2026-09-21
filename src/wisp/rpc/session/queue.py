"""Session queue command execution for the RPC frontend."""

from __future__ import annotations

from typing import assert_never

from wisp.coding import CodingSession
from wisp.events import QueueItemsRemoved, QueueUpdated, RpcCommandFinished, WispEvent
from wisp.rpc.commands import (
    ClearQueueCommand,
    FollowUpCommand,
    GetQueueStateCommand,
    PopQueueCommand,
    SetQueueModeCommand,
    SteerCommand,
)
from wisp.rpc.framing import encode_rpc_frame
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
    max_frame_bytes: int | None = None,
) -> None:
    """Execute one queue command, guarding mutations before removal or publication.

    Args:
        command (_RpcQueueCommand): Validated queue command.
        agent (CodingSession): Authoritative queue owner.
        session (JsonlSession | None): Selected session for idle inspection.
        write_event (RpcEventWriter): Ordered lifecycle and queue event sink.
        max_frame_bytes (int | None): Outbound JSONL ceiling; None for in-process
            transports without serialization. Predictable frame failures leave queues intact.
    """

    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    command_id = lifecycle.command_id

    removed: QueueItemsRemoved | None = None
    try:
        if isinstance(command, (SetQueueModeCommand, PopQueueCommand, ClearQueueCommand)):
            # Guard, response preflight, and mutation deliberately contain no await.
            if command.expected_token is not None:
                agent.validate_queue_token(command.expected_token)
            if max_frame_bytes is not None:
                _preflight_mutation(
                    command, agent.queue_state(session), command_id, max_frame_bytes
                )
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
        state = state.model_copy(update={"command_id": command_id})
        if isinstance(command, GetQueueStateCommand) and max_frame_bytes is not None:
            encode_rpc_frame(state, max_frame_bytes=max_frame_bytes)
    except (RuntimeError, ValueError) as exc:
        lifecycle.fail(str(exc))
        return

    if removed is not None:
        write_event(removed)
    write_event(state)
    lifecycle.finish()


def _preflight_mutation(
    command: SetQueueModeCommand | PopQueueCommand | ClearQueueCommand,
    before: QueueUpdated,
    command_id: str,
    max_frame_bytes: int,
) -> None:
    """Check all mutation responses before irreversibly removing queued text.

    Args:
        command (SetQueueModeCommand | PopQueueCommand | ClearQueueCommand): Mutation.
        before (QueueUpdated): Authoritative state in the synchronous mutation window.
        command_id (str): Resolved lifecycle identity.
        max_frame_bytes (int): Outbound JSONL payload ceiling.

    Raises:
        ValueError: A response cannot fit the transport ceiling.
    """
    after = before.model_copy(update={"command_id": command_id})
    events: list[WispEvent] = []
    if isinstance(command, SetQueueModeCommand):
        after = after.model_copy(update={f"{command.kind}_mode": command.mode})
    else:
        steering = before.steering if command.kind in (None, "steering") else ()
        follow_up = before.follow_up if command.kind in (None, "follow_up") else ()
        if isinstance(command, PopQueueCommand):
            steering, follow_up = steering[-1:], follow_up[-1:]
        events.append(
            QueueItemsRemoved(
                command_id=command_id,
                operation="pop" if isinstance(command, PopQueueCommand) else "clear",
                kind=command.kind,
                steering=steering,
                follow_up=follow_up,
            )
        )
        after = after.model_copy(
            update={
                "steering": before.steering[: len(before.steering) - len(steering)],
                "follow_up": before.follow_up[: len(before.follow_up) - len(follow_up)],
            }
        )
    events.extend(
        [after, RpcCommandFinished(command_id=command_id, command_type=command.type, ok=True)]
    )
    for event in events:
        # New timestamps can have longer fractional seconds. Queue tokens are
        # fixed-width UUID hex strings, so replacing one cannot grow a frame.
        event = event.model_copy(update={"timestamp": event.timestamp.replace(microsecond=999999)})
        encode_rpc_frame(event, max_frame_bytes=max_frame_bytes)
