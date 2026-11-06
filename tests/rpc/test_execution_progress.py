from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from tests.rpc.execution_support import dispatch_parsed
from tests.support.rpc import build_rpc_executor_fixture, guard_rpc_command_serialization
from wisp.coding.tool_progress import LiveEventSink
from wisp.events import (
    ToolExecutionEnded,
    ToolExecutionProgress,
    ToolExecutionStarted,
    WispEvent,
)


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)


def test_prompt_publishes_live_progress_in_the_run_stream(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    async def scenario() -> list[WispEvent]:
        fixture = await build_rpc_executor_fixture(tmp_path)

        async def run_with_progress(prompt: str, **kwargs: object) -> AsyncIterator[WispEvent]:
            sink = kwargs.get("live_event_sink")
            assert callable(sink)
            live: LiveEventSink = sink
            yield ToolExecutionStarted(call_id="call-1", name="bash", arguments={})
            await live(
                ToolExecutionProgress(
                    call_id="call-1",
                    name="bash",
                    sequence=1,
                    elapsed_ms=500,
                    stdout_tail="building\n",
                    stderr_tail="",
                    stdout_bytes=9,
                    stderr_bytes=0,
                )
            )
            yield ToolExecutionEnded(call_id="call-1", name="bash", output="ok", is_error=False)

        monkeypatch.setattr(fixture.agent, "run", run_with_progress)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            await dispatch_parsed(executor, {"id": "prompt-1", "type": "prompt", "prompt": "go"})
            with anyio.fail_after(5):
                while not any(isinstance(e, ToolExecutionEnded) for e in fixture.events):
                    await anyio.sleep(0.01)
            task_group.cancel_scope.cancel()
        return fixture.events

    events = anyio.run(scenario)

    types = [event.type for event in events]
    started = types.index("tool.execution.started")
    progress = types.index("tool.execution.progress")
    assert started < progress < types.index("tool.execution.ended")
