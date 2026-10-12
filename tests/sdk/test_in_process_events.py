"""In-process SDK event delivery: the single consumer, backpressure, and host event ordering."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import anyio
import pytest
from pytest import MonkeyPatch

import wisp.sdk as sdk_module
from tests.support.rpc import guard_rpc_command_serialization
from wisp.config.runtime import WispConfig
from wisp.events import (
    ErrorEvent,
    RpcCommandFinished,
    RpcCommandStarted,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderTextDelta,
)
from wisp.providers.fake import ScriptedProvider
from wisp.rpc import host as rpc_host_module
from wisp.rpc.coordinator import (
    RpcCoordinator,
    _RpcCommandCompleted,
    _RpcInputClosed,
    _RpcRunningCommand,
    _RpcSessionState,
)
from wisp.runtime.api import WispRuntime
from wisp.runtime.extensions import build_runtime
from wisp.sdk import InProcessOptions, InProcessWisp
from wisp.tools.context import ToolContext


def test_in_process_sdk_allows_one_event_consumer_and_idempotent_cleanup(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        controller.events()
        with pytest.raises(RuntimeError, match="only be consumed once"):
            controller.events()
        await controller.aclose()
        await controller.aclose()

        concurrent_controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path / "concurrent"),
            options=InProcessOptions(startup_trusted=True),
        )
        original_aclose = WispRuntime.aclose
        close_started = anyio.Event()
        release_close = anyio.Event()
        second_close_finished = anyio.Event()

        async def delayed_aclose(runtime: WispRuntime) -> None:
            close_started.set()
            await release_close.wait()
            await original_aclose(runtime)

        monkeypatch.setattr(WispRuntime, "aclose", delayed_aclose)

        async def first_close() -> None:
            await concurrent_controller.aclose()

        async def second_close() -> None:
            await concurrent_controller.aclose()
            second_close_finished.set()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(first_close)
            await close_started.wait()
            task_group.start_soon(second_close)
            await anyio.sleep(0)
            assert second_close_finished.is_set() is False
            release_close.set()

    anyio.run(scenario)


def test_in_process_sdk_relays_events_when_consumer_falls_behind(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    delta_count = 1_100
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="scripted"),
                *(ProviderTextDelta(delta="x") for _ in range(delta_count)),
                ProviderResponseCompleted(content="x" * delta_count),
            ]
        ]
    )

    async def build_scripted_runtime(_config: WispConfig) -> WispRuntime:
        runtime = await build_runtime()
        runtime.providers.register(provider)
        return runtime

    monkeypatch.setattr(sdk_module, "build_runtime_for_config", build_scripted_runtime)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="scripted", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        prompt_id = await controller.prompt("burst", command_id="prompt-1")
        # Let the provider pass the bounded consumer buffer before reading it.
        await anyio.sleep(0.05)
        state_id: str | None = None
        shutdown_id: str | None = None
        events = []
        try:
            async for event in controller.events():
                events.append(event)
                if event.type == "message.delta" and state_id is None:
                    # Yield after consuming a delta so the provider refills the
                    # stream reserve. A sole event consumer must still be able
                    # to submit a command without waiting for itself to read
                    # another event.
                    await anyio.sleep(0)
                    with anyio.fail_after(1):
                        state_id = await controller.get_state(command_id="state-1")
                elif isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                    shutdown_id = await controller.shutdown(command_id="shutdown-1")
                elif isinstance(event, RpcCommandFinished) and event.command_id == shutdown_id:
                    break
        finally:
            await controller.aclose()

        assert sum(event.type == "message.delta" for event in events) == delta_count
        assert any(
            isinstance(event, RpcCommandFinished) and event.command_id == prompt_id and event.ok
            for event in events
        )
        assert any(
            isinstance(event, RpcCommandFinished) and event.command_id == state_id and event.ok
            for event in events
        )
        assert state_id is not None
        state_started_index = next(
            index
            for index, event in enumerate(events)
            if isinstance(event, RpcCommandStarted) and event.command_id == state_id
        )
        state_finished_index = next(
            index
            for index, event in enumerate(events)
            if isinstance(event, RpcCommandFinished) and event.command_id == state_id
        )
        assert not any(
            event.type == "message.delta"
            for event in events[state_started_index : state_finished_index + 1]
        )

    anyio.run(scenario)


def test_rpc_host_serializes_worker_event_after_bypass_lifecycle_batch() -> None:
    async def scenario() -> None:
        first_event_rendered = anyio.Event()
        release_batch = anyio.Event()
        rendered: list[Any] = []
        batch = (
            RpcCommandStarted(command_id="state-1", command_type="get_state"),
            ErrorEvent(message="state payload"),
            RpcCommandFinished(command_id="state-1", command_type="get_state", ok=True),
        )
        worker_event = RpcCommandFinished(
            command_id="stats-1",
            command_type="get_session_stats",
            ok=True,
        )

        async def render_events(events: Any) -> None:
            async for event in events:
                rendered.append(event)
                if event is batch[0]:
                    first_event_rendered.set()
                    await release_batch.wait()

        host = rpc_host_module.RpcHost(
            runtime=cast(WispRuntime, object()),
            sessions=cast(Any, object()),
            agent=cast(Any, SimpleNamespace(tool_context=ToolContext(cwd=Path.cwd()))),
            approval_policy=cast(Any, object()),
            trust_gate=cast(Any, object()),
            configure_overrides=cast(Any, object()),
            coordinator=cast(Any, object()),
            write_event=lambda _event: None,
            render_events=render_events,
        )

        async with anyio.create_task_group() as task_group:
            host._event_task_group = task_group
            task_group.start_soon(host._render_event_batch, batch)
            await first_event_rendered.wait()
            host._publish_event(worker_event)
            await anyio.sleep(0)
            assert rendered == [batch[0]]
            release_batch.set()

        host._event_task_group = None
        assert rendered == [*batch, worker_event]

    anyio.run(scenario)


def test_rpc_host_drains_published_completion_events_before_return() -> None:
    async def scenario() -> None:
        render_started = anyio.Event()
        release_render = anyio.Event()
        host_returned = anyio.Event()
        rendered: list[Any] = []
        coordinator = RpcCoordinator(_RpcSessionState(session=None, history=(), entry_count=0))
        host = rpc_host_module.RpcHost(
            runtime=cast(WispRuntime, object()),
            sessions=cast(Any, object()),
            agent=cast(Any, SimpleNamespace(tool_context=ToolContext(cwd=Path.cwd()))),
            approval_policy=cast(Any, object()),
            trust_gate=cast(Any, object()),
            configure_overrides=cast(Any, object()),
            coordinator=coordinator,
            write_event=lambda _event: None,
            render_events=lambda events: render_events(events),
        )
        coordinator._completion_event_writer = host._publish_event
        coordinator.running_command = _RpcRunningCommand(
            command_id="select-1",
            command_type="select_session",
            cancel_scope=anyio.CancelScope(),
        )
        control_send, control_receive = anyio.create_memory_object_stream(2)
        host_send, host_receive = anyio.create_memory_object_stream(1)

        async def render_events(events: Any) -> None:
            async for event in events:
                rendered.append(event)
                render_started.set()
                await release_render.wait()

        async def run_host(task_group: anyio.abc.TaskGroup) -> None:
            assert (
                await host.run_with_streams(
                    control_receive,
                    send=host_send,
                    task_group=task_group,
                )
            ) is False
            host_returned.set()

        async with (
            control_send,
            control_receive,
            host_send,
            host_receive,
            anyio.create_task_group() as task_group,
        ):
            task_group.start_soon(run_host, task_group)
            await control_send.send(
                _RpcCommandCompleted(
                    command_id="select-1",
                    command_type="select_session",
                    ok=True,
                    history=(),
                    entry_count=0,
                    post_apply_events=(ErrorEvent(message="selection applied"),),
                )
            )
            await control_send.send(_RpcInputClosed())
            await render_started.wait()
            await anyio.sleep(0)
            assert host_returned.is_set() is False
            release_render.set()
            await host_returned.wait()

        assert len(rendered) == 1
        assert isinstance(rendered[0], ErrorEvent)
        assert rendered[0].message == "selection applied"

    anyio.run(scenario)


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)
