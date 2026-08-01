"""Deterministic coverage for the public in-process embedding API."""

from __future__ import annotations

from pathlib import Path
from threading import Event, get_ident
from typing import Any, cast

import anyio
import pytest
from pytest import MonkeyPatch

import wisp.sdk as sdk_module
from wisp.agent.messages import Message
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
from wisp.rpc import host as rpc_host_module
from wisp.runtime.api import WispRuntime
from wisp.runtime.extensions import build_runtime
from wisp.sdk import InProcessOptions, InProcessWisp
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore


def test_in_process_sdk_from_environment_offloads_blocking_setup(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    main_thread = get_ident()
    call_threads: dict[str, int] = {}
    expected_config = WispConfig(provider="fake", session_dir=tmp_path / "sessions")

    def resolve_project_root(_cwd: Path) -> Path:
        call_threads["project_root"] = get_ident()
        return tmp_path

    def check_trust(project_root: Path) -> bool:
        assert project_root == tmp_path
        call_threads["trust"] = get_ident()
        return False

    def build_config(
        _overrides: object,
        *,
        trusted: bool,
        project_dir: Path | None = None,
    ) -> WispConfig:
        assert trusted is False
        assert project_dir == tmp_path
        call_threads["config"] = get_ident()
        return expected_config

    async def fake_start(
        cls: type[InProcessWisp],
        config: WispConfig,
        *,
        options: InProcessOptions,
        config_overrides: object | None = None,
    ) -> object:
        assert cls is InProcessWisp
        assert config == expected_config
        assert options.project_context_root == tmp_path
        assert config_overrides is not None
        return expected_config

    monkeypatch.setattr(sdk_module, "resolve_project_context_root", resolve_project_root)
    monkeypatch.setattr(sdk_module, "trusted_noninteractive", check_trust)
    monkeypatch.setattr(sdk_module._ConfigOverrides, "build", build_config)
    monkeypatch.setattr(InProcessWisp, "_start", classmethod(fake_start))

    async def scenario() -> None:
        result = await InProcessWisp.from_environment()
        assert result is expected_config

    anyio.run(scenario)
    assert set(call_threads) == {"project_root", "trust", "config"}
    assert all(thread_id != main_thread for thread_id in call_threads.values())


def test_in_process_sdk_offloads_resumed_session_startup(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    main_thread = get_ident()
    call_threads: dict[str, int] = {}
    sessions = JsonlSessionStore(tmp_path)

    original_select_session = rpc_host_module.select_session
    original_session_state = rpc_host_module.rpc_session_state

    def select_session_in_worker(
        store: JsonlSessionStore,
        *,
        resume: str | None,
        continue_latest: bool,
    ) -> JsonlSession | None:
        call_threads["select"] = get_ident()
        return original_select_session(store, resume=resume, continue_latest=continue_latest)

    def session_state_in_worker(session: JsonlSession | None) -> object:
        call_threads["state"] = get_ident()
        return original_session_state(session)

    monkeypatch.setattr(rpc_host_module, "select_session", select_session_in_worker)
    monkeypatch.setattr(rpc_host_module, "rpc_session_state", session_state_in_worker)

    async def scenario() -> None:
        resumed_session = sessions.create()
        await resumed_session.append_message(Message(role="user", content="resume me"))
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(
                continue_latest=True,
                startup_trusted=True,
                project_context_root=tmp_path,
            ),
        )
        try:
            session_state = controller._in_process_transport._host.coordinator.session_state
            assert session_state.session is not None
            assert session_state.session.path == resumed_session.path
        finally:
            await controller.aclose()

    anyio.run(scenario)
    assert set(call_threads) == {"select", "state"}
    assert all(thread_id != main_thread for thread_id in call_threads.values())


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


def test_in_process_options_allows_zero_tool_iterations() -> None:
    assert InProcessOptions(max_tool_iterations=0).max_tool_iterations == 0
    with pytest.raises(ValueError, match="non-negative"):
        InProcessOptions(max_tool_iterations=-1)


def test_rpc_trust_gate_offloads_store_io(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    main_thread = get_ident()
    call_threads: dict[str, int] = {}

    def check_trust(_project_path: Path) -> None:
        call_threads["read"] = get_ident()
        return None

    def save_trust(_project_path: Path, _trusted: bool) -> None:
        call_threads["write"] = get_ident()

    monkeypatch.setattr(rpc_host_module, "is_trusted", check_trust)
    monkeypatch.setattr(rpc_host_module, "record_trust", save_trust)
    gate = rpc_host_module.RpcTrustGate(tmp_path, write_event=lambda _event: None)

    async def scenario() -> bool:
        decision: bool | None = None

        async def resolve() -> None:
            nonlocal decision
            decision = await gate.resolve()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(resolve)
            while gate._pending is None:
                await anyio.sleep(0)
            assert gate.resolve_request(request_id=gate._pending.request_id, trusted=False)
        assert decision is not None
        return decision

    assert anyio.run(scenario) is False
    assert set(call_threads) == {"read", "write"}
    assert all(thread_id != main_thread for thread_id in call_threads.values())


def test_rpc_trust_gate_cancel_abandons_blocked_persistence(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    started = Event()
    release = Event()

    def check_trust(_project_path: Path) -> None:
        return None

    def save_trust(_project_path: Path, _trusted: bool) -> None:
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(rpc_host_module, "is_trusted", check_trust)
    monkeypatch.setattr(rpc_host_module, "record_trust", save_trust)
    gate = rpc_host_module.RpcTrustGate(tmp_path, write_event=lambda _event: None)

    async def scenario() -> None:
        cancel_scope = anyio.CancelScope()
        cancelled = anyio.Event()

        async def resolve() -> None:
            with cancel_scope:
                try:
                    await gate.resolve()
                except anyio.get_cancelled_exc_class():
                    cancelled.set()
                    raise
            if cancel_scope.cancel_called:
                cancelled.set()

        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(resolve)
                while gate._pending is None:
                    await anyio.sleep(0)
                assert gate.resolve_request(request_id=gate._pending.request_id, trusted=True)
                with anyio.fail_after(1):
                    while not started.is_set():
                        await anyio.sleep(0.01)
                cancel_scope.cancel()
                with anyio.fail_after(1):
                    await cancelled.wait()
        finally:
            release.set()

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
        trust_started_index = next(
            index
            for index, event in enumerate(events)
            if isinstance(event, RpcCommandStarted) and event.command_id == "trust-1"
        )
        trust_finished_index = next(
            index
            for index, event in enumerate(events)
            if isinstance(event, RpcCommandFinished) and event.command_id == "trust-1" and event.ok
        )
        trust_resolved_index = events.index(resolved)
        assert prompt_started_index < trust_requested_index
        assert trust_started_index < trust_finished_index < trust_resolved_index
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


def test_in_process_sdk_close_abandons_blocked_trust_store_read(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    read_started = Event()
    release_read = Event()

    def check_trust(_project_path: Path) -> None:
        read_started.set()
        release_read.wait()
        return None

    monkeypatch.setattr(rpc_host_module, "is_trusted", check_trust)
    monkeypatch.setattr(sdk_module, "_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(project_context_root=tmp_path),
        )
        await controller.prompt("hello", command_id="prompt-1")
        while not read_started.is_set():
            await anyio.sleep(0)
        with anyio.fail_after(0.5):
            await controller.aclose()

    try:
        anyio.run(scenario)
    finally:
        release_read.set()


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
