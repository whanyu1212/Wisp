"""Subagents setting inspection and changes for the RPC frontend."""

from __future__ import annotations

from wisp.coding.session import CodingSession
from wisp.config.runtime import subagents_from_env
from wisp.config.settings import try_persist_user_subagents
from wisp.events import ErrorEvent, RpcSubagentsReported, SubagentsState
from wisp.rpc.commands import GetSubagentsCommand, SetSubagentsCommand
from wisp.rpc.lifecycle import RpcCommandLifecycle, RpcEventWriter


def handle_rpc_subagents_command(
    command: GetSubagentsCommand | SetSubagentsCommand,
    *,
    agent: CodingSession,
    write_event: RpcEventWriter,
) -> None:
    """Report the subagents setting, first applying and saving a new one.

    A new setting applies to the session's next run and is saved as the user
    setting. A failed save leaves it applied for this session and reports an error
    before the snapshot.

    Args:
        command (GetSubagentsCommand | SetSubagentsCommand): The inspection or change.
        agent (CodingSession): The session whose setting is read or changed.
        write_event (RpcEventWriter): Writes the snapshot and lifecycle events.
    """

    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    if isinstance(command, SetSubagentsCommand):
        agent.subagents = command.enabled
        if not try_persist_user_subagents(agent.subagents):
            write_event(
                ErrorEvent(message="Subagents setting applied for this session; could not save it.")
            )
    write_event(RpcSubagentsReported(command_id=lifecycle.command_id, subagents=_snapshot(agent)))
    lifecycle.finish()


def _snapshot(agent: CodingSession) -> SubagentsState:
    return SubagentsState(
        enabled=agent.subagents,
        offered=agent.subagents_offered(),
        env_enabled=subagents_from_env(),
        model=agent.subagent_model,
        model_in_use=agent.subagent_model_in_use(),
    )
