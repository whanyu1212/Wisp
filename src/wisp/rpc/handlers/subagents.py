"""Subagents setting inspection and changes for the RPC frontend."""

from __future__ import annotations

from wisp.coding.session import CodingSession
from wisp.coding.subagent import SUBAGENT_MAX_PARALLEL
from wisp.config.runtime import subagent_model_from_env, subagents_from_env
from wisp.config.settings import try_persist_user_subagent_model, try_persist_user_subagents
from wisp.events import ErrorEvent, RpcSubagentsReported, SubagentsState
from wisp.rpc.commands import (
    GetSubagentsCommand,
    SetSubagentModelCommand,
    SetSubagentsCommand,
)
from wisp.rpc.lifecycle import RpcCommandLifecycle, RpcEventWriter
from wisp.tools.subagent import SUBAGENT_MAX_TASKS, SUBAGENT_REPORT_MAX_BYTES


def handle_rpc_subagents_command(
    command: GetSubagentsCommand | SetSubagentsCommand | SetSubagentModelCommand,
    *,
    agent: CodingSession,
    write_event: RpcEventWriter,
) -> None:
    """Report the subagents settings, first applying and saving a new one.

    A new setting applies to the session's next run and is saved as the user
    setting. A failed save leaves it applied for this session and reports an error
    before the snapshot. A model the session's provider is known not to have fails
    the command and changes nothing.

    Args:
        command (GetSubagentsCommand | SetSubagentsCommand | SetSubagentModelCommand):
            The inspection or change.
        agent (CodingSession): The session whose settings are read or changed.
        write_event (RpcEventWriter): Writes the snapshot and lifecycle events.
    """

    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    if isinstance(command, SetSubagentsCommand):
        agent.subagents = command.enabled
        if not try_persist_user_subagents(agent.subagents):
            write_event(
                ErrorEvent(message="Subagents setting applied for this session; could not save it.")
            )
    elif isinstance(command, SetSubagentModelCommand):
        if command.model is not None and agent.provider_lacks_model(command.model):
            lifecycle.fail(
                f'Provider {agent.provider.name} has no model "{command.model}"; '
                "the subagent model is unchanged"
            )
            return
        agent.subagent_model = command.model
        if not try_persist_user_subagent_model(agent.subagent_model):
            write_event(
                ErrorEvent(message="Subagent model applied for this session; could not save it.")
            )
    write_event(RpcSubagentsReported(command_id=lifecycle.command_id, subagents=_snapshot(agent)))
    lifecycle.finish()


def _snapshot(agent: CodingSession) -> SubagentsState:
    tools, refused_tools = agent.subagent_tool_access()
    return SubagentsState(
        enabled=agent.subagents,
        offered=agent.subagents_offered(),
        env_enabled=subagents_from_env(),
        model=agent.subagent_model,
        model_in_use=agent.subagent_model_in_use(),
        env_model=subagent_model_from_env(),
        tools=tools,
        refused_tools=refused_tools,
        kinds=agent.subagent_kinds(),
        max_tool_rounds=agent.subagent_tool_rounds(),
        max_tasks=SUBAGENT_MAX_TASKS,
        max_parallel=SUBAGENT_MAX_PARALLEL,
        report_max_bytes=SUBAGENT_REPORT_MAX_BYTES,
    )
