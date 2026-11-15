"""Background processes for the user: stopping one, moving a foreground one there,
and listing them and reading their output."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import anyio
import pytest

from tests.tools.shell.supervisor_support import python_command
from wisp.coding.session import CodingSession
from wisp.coding.tool_execution import BackgroundRequests
from wisp.events import (
    RpcCommandFinished,
    RpcProcessesReported,
    RpcProcessOutputReported,
    WispEvent,
)
from wisp.rpc.commands import (
    BackgroundToolCallCommand,
    CancelProcessCommand,
    GetProcessesCommand,
    GetProcessOutputCommand,
)
from wisp.rpc.handlers.processes import (
    handle_background_tool_call,
    handle_get_process_output,
    handle_get_processes,
    start_cancel_process_command,
)
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


class _Agent:
    def __init__(self, running: set[str]) -> None:
        self.running = running

    def background_tool_call(self, call_id: str) -> bool:
        return call_id in self.running


@pytest.mark.parametrize(("call_id", "ok"), [("long", True), ("other", False)])
def test_background_tool_call(call_id: str, ok: bool) -> None:
    """Only a running foreground call can be moved; any other id fails clearly."""

    events: list[WispEvent] = []

    handle_background_tool_call(
        BackgroundToolCallCommand(id="move-1", call_id=call_id),
        agent=cast(CodingSession, _Agent({"long"})),
        write_event=events.append,
    )

    (finished,) = [e for e in events if isinstance(e, RpcCommandFinished)]
    assert finished.ok is ok
    if not ok:
        assert finished.error == "No running foreground command has the call id other"


def test_background_requests_track_running_calls() -> None:
    requests = BackgroundRequests()
    assert requests.request("call") is False

    moved = requests.register("call")
    assert requests.request("call") is True
    assert moved.is_set()

    requests.release("call")
    assert requests.request("call") is False


@pytest.mark.process
@pytest.mark.parametrize("known", [True, False])
def test_get_process_output(tmp_path: Path, known: bool) -> None:
    """The output read reports the process and its output; an unknown id fails by name."""

    async def scenario() -> list[WispEvent]:
        events: list[WispEvent] = []
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(
                python_command("print('ready', flush=True); import time; time.sleep(30)"),
                cwd=tmp_path,
                timeout=60,
            )
            with anyio.fail_after(10):
                while not supervisor.output_tail(process_id)[1].chunks:
                    await anyio.sleep(0.01)
            handle_get_processes(
                GetProcessesCommand(id="list-1"), supervisor=supervisor, write_event=events.append
            )
            handle_get_process_output(
                GetProcessOutputCommand(
                    id="read-1", process_id=process_id if known else "process-gone"
                ),
                supervisor=supervisor,
                write_event=events.append,
            )
            return events
        finally:
            await supervisor.aclose()

    events = anyio.run(scenario)

    (listing,) = [e for e in events if isinstance(e, RpcProcessesReported)]
    (process,) = listing.processes
    assert (process.state, process.ended_at_ms) == ("running", None)
    finished = {e.command_id: e for e in events if isinstance(e, RpcCommandFinished)}
    assert finished["list-1"].ok
    outputs = [e for e in events if isinstance(e, RpcProcessOutputReported)]
    if known:
        assert finished["read-1"].ok
        (output,) = outputs
        assert output.process == process
        assert [(chunk.stream, chunk.text) for chunk in output.chunks] == [("stdout", "ready\n")]
        assert output.omitted_bytes == 0
    else:
        assert not finished["read-1"].ok
        assert finished["read-1"].error is not None
        assert "process-gone" in finished["read-1"].error
        assert outputs == []
