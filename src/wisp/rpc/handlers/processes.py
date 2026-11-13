"""Stop a background process on the user's behalf."""

from __future__ import annotations

import anyio
from anyio.abc import TaskGroup

from wisp.rpc.commands import CancelProcessCommand
from wisp.rpc.lifecycle import RpcCommandLifecycle, RpcEventWriter
from wisp.tools.result import ToolError
from wisp.tools.shell.supervisor import ProcessSupervisor


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
