"""Background processes stop when the session that started them is left."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from wisp.config.runtime import WispConfig
from wisp.events import ErrorEvent, KnownWispEvent, RpcCommandFinished
from wisp.messages import Message
from wisp.sdk import InProcessOptions, InProcessWisp
from wisp.tools.shell import supervisor as supervisor_module
from wisp.tools.shell.supervisor import ProcessSupervisor


@pytest.mark.parametrize(
    ("change", "state"),
    [
        pytest.param("new", "cancelled", id="new-session"),
        pytest.param("other", "cancelled", id="select-other"),
        pytest.param("same", "running", id="select-same"),
    ],
)
def test_leaving_session_stops_processes(tmp_path: Path, change: str, state: str) -> None:
    async def scenario() -> str:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        host = controller._in_process_transport._host
        supervisor: ProcessSupervisor = host.runtime.process_supervisor
        current = host.sessions.create()
        await current.append_message(Message(role="user", content="current"))
        other = host.sessions.create()
        await other.append_message(Message(role="user", content="other"))
        events = aiter(controller.events())
        try:
            select_id = await controller.select_session(current.session_id)
            await _finished(events, select_id)
            process_id = await supervisor.start("sleep 30", cwd=tmp_path, timeout=60)

            if change == "new":
                command_id = await controller.new_session()
            else:
                target = other if change == "other" else current
                command_id = await controller.select_session(target.session_id)
            await _finished(events, command_id)
            # Commands run one at a time, so the next one starts after the processes
            # of the session that was left have stopped.
            await _finished(events, await controller.get_state())

            return (await supervisor.poll(process_id)).state
        finally:
            await controller.aclose()

    assert anyio.run(scenario) == state


def test_reports_process_it_cannot_stop(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    original_terminate = supervisor_module._terminate_process_tree

    async def fail_terminate(process: object) -> bool:
        await original_terminate(process)  # type: ignore[arg-type]
        return False

    monkeypatch.setattr(supervisor_module, "_terminate_process_tree", fail_terminate)

    async def scenario() -> tuple[str, list[str]]:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        supervisor: ProcessSupervisor = (
            controller._in_process_transport._host.runtime.process_supervisor
        )
        events = aiter(controller.events())
        errors: list[str] = []
        try:
            process_id = await supervisor.start("sleep 30", cwd=tmp_path, timeout=60)
            await _finished(events, await controller.new_session(), errors=errors)
            await _finished(events, await controller.get_state(), errors=errors)
            return process_id, errors
        finally:
            monkeypatch.setattr(supervisor_module, "_terminate_process_tree", original_terminate)
            await controller.aclose()

    process_id, errors = anyio.run(scenario)

    assert len(errors) == 1
    assert "Could not stop 1 background process from the previous session" in errors[0]
    assert process_id in errors[0]


async def _finished(
    events: AsyncIterator[KnownWispEvent], command_id: str, *, errors: list[str] | None = None
) -> None:
    with anyio.fail_after(10):
        async for event in events:
            if isinstance(event, ErrorEvent) and errors is not None:
                errors.append(event.message)
            if isinstance(event, RpcCommandFinished) and event.command_id == command_id:
                assert event.ok, event.error
                return
