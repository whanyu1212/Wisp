"""Stopping a background process on the user's behalf through the RPC command."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from tests.tools.shell.supervisor_support import python_command
from wisp.events import RpcCommandFinished, WispEvent
from wisp.rpc.commands import CancelProcessCommand
from wisp.rpc.handlers.processes import start_cancel_process_command
from wisp.tools.shell.supervisor import ProcessSupervisor


@pytest.mark.process
@pytest.mark.parametrize("target", ["running", "unknown", "finished"])
def test_cancel_process(tmp_path: Path, target: str) -> None:
    """Only a running background process can be stopped; others fail clearly."""

    async def scenario() -> tuple[list[WispEvent], tuple[str, ...]]:
        events: list[WispEvent] = []
        supervisor = ProcessSupervisor()
        try:
            sleeper = await supervisor.start(
                python_command("import time; time.sleep(30)"), cwd=tmp_path, timeout=60
            )
            quick = await supervisor.start(python_command("pass"), cwd=tmp_path, timeout=60)
            await supervisor.poll(quick, wait_seconds=10, until="exit")
            process_id = {"running": sleeper, "unknown": "process-gone", "finished": quick}[target]
            async with anyio.create_task_group() as task_group:
                start_cancel_process_command(
                    CancelProcessCommand(id="stop-1", process_id=process_id),
                    supervisor=supervisor,
                    task_group=task_group,
                    write_event=events.append,
                )
            still_running = tuple(p.process_id for p in supervisor.running_processes())
            return events, still_running
        finally:
            await supervisor.aclose()

    events, still_running = anyio.run(scenario)

    (finished,) = [e for e in events if isinstance(e, RpcCommandFinished)]
    assert finished.command_type == "cancel_process"
    if target == "running":
        assert finished.ok
        assert still_running == ()
    else:
        assert not finished.ok
        assert finished.error is not None
        assert "No running background process" in finished.error
        assert len(still_running) == 1
