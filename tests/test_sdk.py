"""Deterministic coverage for the public in-process embedding API."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from pytest import MonkeyPatch

import wisp.sdk as sdk_module
from wisp.config import WispConfig
from wisp.events import (
    RpcCommandFinished,
    RpcCommandStarted,
    RpcStateReported,
    TrustRequested,
    TrustResolved,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderTextDelta,
)
from wisp.providers.fake import ScriptedProvider
from wisp.runtime.api import WispRuntime
from wisp.runtime.extensions import build_runtime
from wisp.sdk import InProcessOptions, InProcessWisp


def test_in_process_sdk_rejects_non_asyncio_backends_before_startup(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        sdk_sniffio = cast(Any, sdk_module).sniffio
        monkeypatch.setattr(sdk_sniffio, "current_async_library", lambda: "trio")
        with pytest.raises(RuntimeError, match="requires AnyIO's asyncio backend"):
            await InProcessWisp.start(WispConfig(provider="fake", session_dir=tmp_path))

    anyio.run(scenario)


def test_in_process_sdk_runs_the_shared_command_event_contract(tmp_path: Path) -> None:
    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        prompt_id = await controller.prompt("hello", command_id="prompt-1")
        state_id: str | None = None
        shutdown_id: str | None = None
        events = []
        try:
            async for event in controller.events():
                events.append(event)
                if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                    state_id = await controller.get_state(command_id="state-1")
                elif isinstance(event, RpcStateReported) and event.command_id == state_id:
                    shutdown_id = await controller.shutdown(command_id="shutdown-1")
                elif isinstance(event, RpcCommandFinished) and event.command_id == shutdown_id:
                    break
        finally:
            await controller.aclose()

        assert [event.command_id for event in events if isinstance(event, RpcCommandStarted)] == [
            "prompt-1",
            "state-1",
            "shutdown-1",
        ]
        assert any(event.type == "message.delta" for event in events)
        state = next(event for event in events if isinstance(event, RpcStateReported))
        assert state.state.provider == "fake"
        assert state.state.session_id is not None

    anyio.run(scenario)


def test_in_process_sdk_surfaces_and_resolves_project_trust(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("WISP_PROVIDER", "fake")
    monkeypatch.setenv("WISP_TRUST_FILE", str(tmp_path / "trust.json"))
    monkeypatch.delenv("WISP_TRUST", raising=False)

    async def scenario() -> None:
        controller = await InProcessWisp.from_environment(
            session_dir=tmp_path / "sessions",
            options=InProcessOptions(project_context_root=tmp_path),
        )
        prompt_id = await controller.prompt("hello", command_id="prompt-1")
        shutdown_id: str | None = None
        events = []
        try:
            async for event in controller.events():
                events.append(event)
                if isinstance(event, TrustRequested):
                    await controller.trust(
                        event.request_id,
                        trusted=False,
                        command_id="trust-1",
                    )
                elif isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                    shutdown_id = await controller.shutdown(command_id="shutdown-1")
                elif isinstance(event, RpcCommandFinished) and event.command_id == shutdown_id:
                    break
        finally:
            await controller.aclose()

        resolved = next(event for event in events if isinstance(event, TrustResolved))
        assert resolved.trusted is False
        trust_requested_index = next(
            index for index, event in enumerate(events) if isinstance(event, TrustRequested)
        )
        prompt_started_index = next(
            index
            for index, event in enumerate(events)
            if isinstance(event, RpcCommandStarted) and event.command_id == prompt_id
        )
        assert prompt_started_index < trust_requested_index
        assert any(
            isinstance(event, RpcCommandFinished) and event.command_id == "trust-1" and event.ok
            for event in events
        )
        assert any(
            isinstance(event, RpcCommandFinished) and event.command_id == prompt_id and event.ok
            for event in events
        )

    anyio.run(scenario)


def test_in_process_sdk_close_denies_pending_trust_without_hanging(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("WISP_PROVIDER", "fake")
    monkeypatch.setenv("WISP_TRUST_FILE", str(tmp_path / "trust.json"))
    monkeypatch.delenv("WISP_TRUST", raising=False)

    async def scenario() -> None:
        controller = await InProcessWisp.from_environment(
            session_dir=tmp_path / "sessions",
            options=InProcessOptions(project_context_root=tmp_path),
        )
        await controller.prompt("hello", command_id="prompt-1")
        try:
            async for event in controller.events():
                if isinstance(event, TrustRequested):
                    break
        finally:
            await controller.aclose()

    anyio.run(scenario)


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

    anyio.run(scenario)


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


def test_sdk_and_host_do_not_depend_on_cli_modules() -> None:
    sdk_path = sdk_module.__file__
    host_module = __import__("wisp.rpc.host", fromlist=["host"])
    host_path = host_module.__file__
    assert sdk_path is not None
    assert host_path is not None

    sdk_source = Path(sdk_path).read_text(encoding="utf-8")
    host_source = Path(host_path).read_text(encoding="utf-8")

    assert "wisp.cli" not in sdk_source
    assert "wisp.cli" not in host_source
