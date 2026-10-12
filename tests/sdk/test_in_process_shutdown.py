"""In-process SDK close and shutdown: command admission, queued shutdown, and cleanup."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from pytest import MonkeyPatch

import wisp.sdk as sdk_module
from tests.support.rpc import guard_rpc_command_serialization
from wisp.config.runtime import WispConfig
from wisp.events import (
    RpcCommandFinished,
    RpcStateReported,
    TrustRequested,
)
from wisp.rpc import host as rpc_host_module
from wisp.rpc.coordinator import (
    _RpcInputClosed,
    _RpcInputCommand,
)
from wisp.runtime.api import WispRuntime
from wisp.sdk import InProcessOptions, InProcessWisp


@pytest.mark.production_fault
def test_in_process_sdk_close_reports_owner_that_resists_cancellation(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(sdk_module, "_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        transport = controller._in_process_transport
        real_cancel_scope = transport._owner_cancel_scope
        real_control_send = transport._control_send

        class ResistantCancelScope:
            def cancel(self) -> None:
                pass

        class BlockedInputClose:
            async def send(self, event: object) -> None:
                await anyio.Event().wait()

            async def aclose(self) -> None:
                await real_control_send.aclose()

        transport._owner_cancel_scope = cast(Any, ResistantCancelScope())
        transport._control_send = cast(Any, BlockedInputClose())
        with pytest.raises(RuntimeError, match="owner did not stop after cancellation"):
            await controller.aclose()
        transport._owner_cancel_scope = real_cancel_scope
        transport._control_send = real_control_send
        await controller.aclose()

    anyio.run(scenario)


def test_in_process_sdk_rejects_command_racing_with_close(tmp_path: Path) -> None:
    async def scenario() -> None:
        command_send_started = anyio.Event()
        command_failed = anyio.Event()
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        transport = controller._in_process_transport
        original_control_send = transport._control_send

        class DelayedCommandSend:
            async def send(self, event: object) -> None:
                if event.__class__.__name__ == "_RpcInputCommand":
                    command_send_started.set()
                    await anyio.Event().wait()
                await original_control_send.send(cast(Any, event))

            async def aclose(self) -> None:
                await original_control_send.aclose()

        transport._control_send = cast(Any, DelayedCommandSend())

        async def submit_command() -> None:
            with pytest.raises(RuntimeError, match="controller is closed"):
                await controller.prompt("racing command", command_id="racing-prompt")
            command_failed.set()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(submit_command)
            await command_send_started.wait()
            with anyio.fail_after(0.5):
                await controller.aclose()
            with anyio.fail_after(0.5):
                await command_failed.wait()

    anyio.run(scenario)


def test_in_process_sdk_rejects_commands_submitted_after_shutdown(tmp_path: Path) -> None:
    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        try:
            await controller.shutdown(command_id="shutdown-1")
            with pytest.raises(RuntimeError, match="controller is closed"):
                await controller.prompt("after shutdown", command_id="prompt-1")
        finally:
            await controller.aclose()

    anyio.run(scenario)


def test_in_process_sdk_allows_trust_resolution_while_shutdown_is_queued(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(project_context_root=tmp_path),
        )
        prompt_id = await controller.prompt("hello", command_id="prompt-1")
        shutdown_id: str | None = None
        trust_id: str | None = None
        events = []
        try:
            async for event in controller.events():
                events.append(event)
                if isinstance(event, TrustRequested):
                    shutdown_id = await controller.shutdown(command_id="shutdown-1")
                    trust_id = await controller.trust(
                        event.request_id,
                        trusted=False,
                        command_id="trust-1",
                    )
                elif (
                    shutdown_id is not None
                    and isinstance(event, RpcCommandFinished)
                    and event.command_id == shutdown_id
                ):
                    break
        finally:
            await controller.aclose()

        assert trust_id is not None
        assert any(
            isinstance(event, RpcCommandFinished) and event.command_id == trust_id and event.ok
            for event in events
        )
        assert any(
            isinstance(event, RpcCommandFinished) and event.command_id == prompt_id and event.ok
            for event in events
        )
        assert any(
            isinstance(event, RpcCommandFinished) and event.command_id == shutdown_id and event.ok
            for event in events
        )

    anyio.run(scenario)


def test_in_process_sdk_reopens_admission_after_queued_shutdown_is_cancelled(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(project_context_root=tmp_path),
        )
        shutdown_id: str | None = None
        cancel_id: str | None = None
        state_id: str | None = None
        events = []
        try:
            await controller.prompt("hello", command_id="prompt-1")
            async for event in controller.events():
                events.append(event)
                if isinstance(event, TrustRequested):
                    shutdown_id = await controller.shutdown(command_id="shutdown-1")
                    cancel_id = await controller.cancel(shutdown_id, command_id="cancel-1")
                elif (
                    cancel_id is not None
                    and isinstance(event, RpcCommandFinished)
                    and event.command_id == cancel_id
                ):
                    transport = controller._in_process_transport
                    with anyio.fail_after(0.5):
                        while transport._shutdown_pending:
                            await anyio.sleep(0)
                    state_id = await controller.get_state(command_id="state-1")
                elif (
                    state_id is not None
                    and isinstance(event, RpcStateReported)
                    and event.command_id == state_id
                ):
                    break
        finally:
            await controller.aclose()

        assert shutdown_id is not None
        assert state_id is not None
        assert any(
            isinstance(event, RpcCommandFinished)
            and event.command_id == shutdown_id
            and not event.ok
            for event in events
        )

    anyio.run(scenario)


@pytest.mark.slow
def test_in_process_sdk_reopens_admission_after_shutdown_queue_rejection(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(project_context_root=tmp_path),
        )
        shutdown_id: str | None = None
        state_id: str | None = None
        events = []
        try:
            await controller.prompt("hello", command_id="prompt-1")
            async for event in controller.events():
                events.append(event)
                if isinstance(event, TrustRequested):
                    for index in range(rpc_host_module._MAX_QUEUED_RPC_COMMANDS):
                        await controller.prompt(f"queued {index}", command_id=f"queued-{index}")
                    shutdown_id = await controller.shutdown(command_id="shutdown-1")
                elif (
                    shutdown_id is not None
                    and isinstance(event, RpcCommandFinished)
                    and event.command_id == shutdown_id
                ):
                    assert event.ok is False
                    transport = controller._in_process_transport
                    with anyio.fail_after(0.5):
                        while transport._shutdown_pending:
                            await anyio.sleep(0)
                    state_id = await controller.get_state(command_id="state-1")
                elif (
                    state_id is not None
                    and isinstance(event, RpcStateReported)
                    and event.command_id == state_id
                ):
                    break
        finally:
            await controller.aclose()

        assert shutdown_id is not None
        assert state_id is not None

    anyio.run(scenario)


def test_in_process_sdk_cancelled_shutdown_keeps_command_admission_open(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        shutdown_send_started = anyio.Event()
        shutdown_cancelled = anyio.Event()
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        transport = controller._in_process_transport
        original_control_send = transport._control_send
        cancellation_scope = anyio.CancelScope()

        class DelayedShutdownSend:
            async def send(self, event: _RpcInputCommand | _RpcInputClosed) -> None:
                if isinstance(event, _RpcInputCommand) and event.command.command_type == "shutdown":
                    shutdown_send_started.set()
                    await anyio.Event().wait()
                await original_control_send.send(event)

            async def aclose(self) -> None:
                await original_control_send.aclose()

        transport._control_send = cast(Any, DelayedShutdownSend())

        async def submit_shutdown() -> None:
            with cancellation_scope:
                await controller.shutdown(command_id="shutdown-1")
            if cancellation_scope.cancel_called:
                shutdown_cancelled.set()

        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(submit_shutdown)
                await shutdown_send_started.wait()
                cancellation_scope.cancel()
                with anyio.fail_after(0.5):
                    await shutdown_cancelled.wait()
                prompt_id = await controller.prompt(
                    "still accepted",
                    command_id="prompt-1",
                )
                assert prompt_id == "prompt-1"
        finally:
            await controller.aclose()

    anyio.run(scenario)


def test_in_process_sdk_retries_runtime_cleanup_after_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    calls = 0

    async def flaky_aclose(_runtime: WispRuntime) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("cleanup failed")

    monkeypatch.setattr(WispRuntime, "aclose", flaky_aclose)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )

        with pytest.raises(RuntimeError, match="cleanup failed"):
            await controller.aclose()

        await controller.aclose()

    anyio.run(scenario)
    assert calls == 2


def test_in_process_sdk_cleanup_is_safe_from_nested_or_other_task(tmp_path: Path) -> None:
    async def start_controller() -> InProcessWisp:
        return await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )

    async def scenario() -> None:
        nested_controller = await start_controller()
        # The controller's owner task, not this nested cancel scope, owns its
        # internal task group. Exiting here must not violate AnyIO's LIFO scope
        # rules.
        with anyio.CancelScope():
            await nested_controller.aclose()

        other_task_controller = await start_controller()
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(other_task_controller.aclose)

    anyio.run(scenario)


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)
