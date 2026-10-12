"""In-process SDK project trust: the RPC trust gate, trust prompts, and trust store failures."""

from __future__ import annotations

from pathlib import Path
from threading import Event, get_ident

import anyio
import pytest
from pytest import MonkeyPatch

import wisp.sdk as sdk_module
from tests.support.rpc import guard_rpc_command_serialization
from wisp.config.runtime import WispConfig
from wisp.events import (
    RpcCommandFinished,
    RpcCommandStarted,
    TrustRequested,
    TrustResolved,
)
from wisp.rpc import host as rpc_host_module
from wisp.sdk import InProcessOptions, InProcessWisp


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
            with anyio.fail_after(2):
                while gate._pending is None:
                    await anyio.sleep(0)
            pending = gate._pending
            assert pending is not None
            assert gate.resolve_request(request_id=pending.request_id, trusted=False)
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
                with anyio.fail_after(2):
                    while gate._pending is None:
                        await anyio.sleep(0)
                pending = gate._pending
                assert pending is not None
                assert gate.resolve_request(request_id=pending.request_id, trusted=True)
                with anyio.fail_after(1):
                    while not started.is_set():
                        await anyio.sleep(0.01)
                cancel_scope.cancel()
                with anyio.fail_after(1):
                    await cancelled.wait()
        finally:
            release.set()

    anyio.run(scenario)


def test_in_process_sdk_reports_trust_persistence_errors_through_prompt(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    def check_trust(_project_path: Path) -> None:
        return None

    def save_trust(_project_path: Path, _trusted: bool) -> None:
        raise OSError("trust store unavailable")

    monkeypatch.setattr(rpc_host_module, "is_trusted", check_trust)
    monkeypatch.setattr(rpc_host_module, "record_trust", save_trust)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(project_context_root=tmp_path),
        )
        prompt_id = await controller.prompt("hello", command_id="prompt-1")
        events = []
        try:
            async for event in controller.events():
                events.append(event)
                if isinstance(event, TrustRequested):
                    await controller.trust(
                        event.request_id,
                        trusted=True,
                        command_id="trust-1",
                    )
                elif isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                    break
        finally:
            await controller.aclose()

        prompt_finished = next(
            event
            for event in events
            if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id
        )
        assert prompt_finished.ok is False
        assert prompt_finished.error == "trust store unavailable"
        assert not any(event.type == "message.delta" for event in events)

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


@pytest.mark.production_fault
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


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)
