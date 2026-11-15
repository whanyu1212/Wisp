"""Background processes on the user's behalf: list them, read their output, stop one,
or move a foreground command there."""

from __future__ import annotations

import anyio
from anyio.abc import TaskGroup

from wisp.coding.session import CodingSession
from wisp.events import RpcProcessesReported, RpcProcessOutputChunk, RpcProcessOutputReported
from wisp.rpc.commands import (
    BackgroundToolCallCommand,
    CancelProcessCommand,
    GetProcessesCommand,
    GetProcessOutputCommand,
)
from wisp.rpc.lifecycle import RpcCommandLifecycle, RpcEventWriter
from wisp.rpc.session.process_exits import process_snapshot
from wisp.tools.result import ToolError
from wisp.tools.shell.supervisor import ProcessSupervisor


def handle_get_processes(
    command: GetProcessesCommand,
    *,
    supervisor: ProcessSupervisor,
    write_event: RpcEventWriter,
) -> None:
    """Report the session's background processes, running or ended.

    Args:
        command (GetProcessesCommand): The command asking for them.
        supervisor (ProcessSupervisor): The supervisor that started them.
        write_event (RpcEventWriter): Writes the reply and the command's lifecycle.
    """

    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    processes = tuple(process_snapshot(process) for process in supervisor.processes())
    write_event(RpcProcessesReported(command_id=lifecycle.command_id, processes=processes))
    lifecycle.finish()


def handle_get_process_output(
    command: GetProcessOutputCommand,
    *,
    supervisor: ProcessSupervisor,
    write_event: RpcEventWriter,
) -> None:
    """Report one background process and the latest output it printed.

    The read drains nothing, so the agent's next poll is unchanged. The command
    fails when the session has no background process with this id.

    Args:
        command (GetProcessOutputCommand): The command naming the process.
        supervisor (ProcessSupervisor): The supervisor that started it.
        write_event (RpcEventWriter): Writes the reply and the command's lifecycle.
    """

    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    try:
        process, tail = supervisor.output_tail(command.process_id)
    except ToolError as exc:
        lifecycle.fail(str(exc))
        return
    write_event(
        RpcProcessOutputReported(
            command_id=lifecycle.command_id,
            process=process_snapshot(process),
            chunks=tuple(
                RpcProcessOutputChunk(stream=chunk.stream, text=chunk.text) for chunk in tail.chunks
            ),
            omitted_bytes=tail.omitted_bytes,
        )
    )
    lifecycle.finish()


def start_cancel_process_command(
    command: CancelProcessCommand,
    *,
    supervisor: ProcessSupervisor,
    task_group: TaskGroup,
    write_event: RpcEventWriter,
) -> None:
    """Start stopping one running background process, beside any active command.

    Stopping a process tree can take seconds, so it finishes in ``task_group``
    rather than holding up the commands behind it. The supervisor announces the
    stop as an exit, which publishes ``process.ended`` with state ``cancelled``, and
    leaves the process's output and final state for the agent's next poll. The
    command always finishes, failing if the process could not be stopped.

    Args:
        command (CancelProcessCommand): The command naming the process.
        supervisor (ProcessSupervisor): The supervisor that started it.
        task_group (TaskGroup): Runs the stop after the command's start is written.
        write_event (RpcEventWriter): Writes the command's lifecycle events.
    """

    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    running = {process.process_id for process in supervisor.running_processes()}
    if command.process_id not in running:
        lifecycle.fail(f"No running background process has the id {command.process_id}")
        return

    async def stop() -> None:
        process_id = command.process_id
        try:
            state = await supervisor.stop(process_id)
        except ToolError as exc:
            # It was released between the check and the stop.
            lifecycle.fail(str(exc))
            return
        except anyio.get_cancelled_exc_class():
            lifecycle.fail(f"Stopping background process {process_id} was interrupted")
            raise
        except Exception as exc:  # noqa: BLE001 - the command must always finish
            lifecycle.fail(f"Could not stop background process {process_id}: {exc}")
            return
        if state == "cancelled":
            lifecycle.finish()
        elif state == "failed":
            lifecycle.fail(f"Could not stop every process started by {process_id}")
        else:
            lifecycle.fail(f"Background process {process_id} had already stopped ({state})")

    task_group.start_soon(stop)


def handle_background_tool_call(
    command: BackgroundToolCallCommand,
    *,
    agent: CodingSession,
    write_event: RpcEventWriter,
) -> None:
    """Ask a running foreground ``bash`` call to move to the background.

    The command finishes as soon as the request is made; the call then ends with
    the id of the background process, unless its command finishes first.

    Args:
        command (BackgroundToolCallCommand): The command naming the call.
        agent (CodingSession): The session running the call.
        write_event (RpcEventWriter): Writes the command's lifecycle events.
    """

    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    if agent.background_tool_call(command.call_id):
        lifecycle.finish()
    else:
        lifecycle.fail(f"No running foreground command has the call id {command.call_id}")
