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
from wisp.auth.storage import ApiKeyCredential, JsonAuthStore
from wisp.config import WispConfig
from wisp.events import (
    ErrorEvent,
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
from wisp.rpc import execution as rpc_execution_module
from wisp.rpc import host as rpc_host_module
from wisp.rpc.commands import ParsedRpcCommand, StoreApiKeyCommand
from wisp.rpc.coordinator import (
    RpcCoordinator,
    _RpcCommandCompleted,
    _RpcInputClosed,
    _RpcInputCommand,
    _RpcRunningCommand,
    _RpcSessionState,
)
from wisp.runtime.api import WispRuntime
from wisp.runtime.extensions import build_runtime
from wisp.sdk import InProcessOptions, InProcessWisp
from wisp.sessions.entries import MessageSessionEntry
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore
from wisp.tools.file_ops import ReadTool


def test_in_process_sdk_start_cancel_abandons_startup_trust_lookup(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    started = Event()
    release = Event()

    def blocking_trusted(_project_path: Path) -> bool:
        started.set()
        release.wait(timeout=5)
        return False

    monkeypatch.setattr(sdk_module, "trusted_noninteractive", blocking_trusted)

    async def scenario() -> None:
        cancel_scope = anyio.CancelScope()
        cancelled = anyio.Event()

        async def start_controller() -> None:
            with cancel_scope:
                try:
                    await InProcessWisp.from_environment(
                        session_dir=tmp_path / "sessions",
                        options=InProcessOptions(project_context_root=tmp_path),
                    )
                except anyio.get_cancelled_exc_class():
                    cancelled.set()
                    raise
            if cancel_scope.cancel_called:
                cancelled.set()

        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(start_controller)
                with anyio.fail_after(1):
                    while not started.is_set():
                        await anyio.sleep(0.01)
                cancel_scope.cancel()
                with anyio.fail_after(1):
                    await cancelled.wait()
        finally:
            release.set()

    anyio.run(scenario)


def test_in_process_sdk_from_environment_offloads_blocking_setup(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    main_thread = get_ident()
    call_threads: dict[str, int] = {}
    expected_config = WispConfig(provider="fake", session_dir=tmp_path / "sessions")
    workspace = Path.home() / "workspace"
    workspace.mkdir()

    original_resolve_startup_paths = sdk_module._resolve_startup_paths

    def resolve_startup_paths(options: InProcessOptions) -> tuple[Path, Path]:
        call_threads["paths"] = get_ident()
        return original_resolve_startup_paths(options)

    def resolve_project_root(cwd: Path) -> Path:
        assert cwd == workspace
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
        assert options.cwd == workspace
        assert config_overrides is not None
        return expected_config

    monkeypatch.setattr(sdk_module, "_resolve_startup_paths", resolve_startup_paths)
    monkeypatch.setattr(sdk_module, "resolve_project_context_root", resolve_project_root)
    monkeypatch.setattr(sdk_module, "trusted_noninteractive", check_trust)
    monkeypatch.setattr(sdk_module._ConfigOverrides, "build", build_config)
    monkeypatch.setattr(InProcessWisp, "_start", classmethod(fake_start))

    async def scenario() -> None:
        result = await InProcessWisp.from_environment(
            options=InProcessOptions(cwd=Path("~/workspace"))
        )
        assert result is expected_config

    anyio.run(scenario)
    assert set(call_threads) == {"paths", "project_root", "trust", "config"}
    assert all(thread_id != main_thread for thread_id in call_threads.values())


def test_in_process_sdk_offloads_explicit_workspace_path_normalization(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    main_thread = get_ident()
    call_thread: int | None = None
    expected_config = WispConfig(provider="fake", session_dir=tmp_path / "sessions")
    workspace = tmp_path / "workspace"

    original_resolve_startup_paths = sdk_module._resolve_startup_paths

    def resolve_startup_paths(options: InProcessOptions) -> tuple[Path, Path]:
        nonlocal call_thread
        call_thread = get_ident()
        return original_resolve_startup_paths(options)

    async def fake_start(
        cls: type[InProcessWisp],
        config: WispConfig,
        *,
        options: InProcessOptions,
        config_overrides: object | None = None,
    ) -> object:
        assert cls is InProcessWisp
        assert config == expected_config
        assert options.project_context_root == workspace
        assert options.cwd == workspace
        assert config_overrides is not None
        return expected_config

    monkeypatch.setattr(sdk_module, "_resolve_startup_paths", resolve_startup_paths)
    monkeypatch.setattr(sdk_module, "trusted_noninteractive", lambda _path: False)
    monkeypatch.setattr(
        sdk_module._ConfigOverrides,
        "build",
        lambda _overrides, **_kwargs: expected_config,
    )
    monkeypatch.setattr(InProcessWisp, "_start", classmethod(fake_start))

    async def scenario() -> None:
        result = await InProcessWisp.from_environment(
            options=InProcessOptions(project_context_root=workspace)
        )
        assert result is expected_config

    anyio.run(scenario)
    assert call_thread is not None
    assert call_thread != main_thread


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


def test_in_process_sdk_start_cancel_abandons_resumed_session_replay(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    started = Event()
    release = Event()
    sessions = JsonlSessionStore(tmp_path)
    original_session_state = rpc_host_module.rpc_session_state

    def blocked_session_state(session: JsonlSession | None) -> object:
        started.set()
        release.wait(timeout=5)
        return original_session_state(session)

    monkeypatch.setattr(rpc_host_module, "rpc_session_state", blocked_session_state)

    async def scenario() -> None:
        resumed_session = sessions.create()
        await resumed_session.append_message(Message(role="user", content="resume me"))
        cancel_scope = anyio.CancelScope()
        cancelled = anyio.Event()

        async def start_controller() -> None:
            with cancel_scope:
                try:
                    await InProcessWisp.start(
                        WispConfig(provider="fake", session_dir=tmp_path),
                        options=InProcessOptions(
                            continue_latest=True,
                            startup_trusted=True,
                            project_context_root=tmp_path,
                        ),
                    )
                except anyio.get_cancelled_exc_class():
                    cancelled.set()
                    raise
            if cancel_scope.cancel_called:
                cancelled.set()

        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(start_controller)
                with anyio.fail_after(1):
                    while not started.is_set():
                        await anyio.sleep(0.01)
                cancel_scope.cancel()
                with anyio.fail_after(1):
                    await cancelled.wait()
        finally:
            release.set()

    anyio.run(scenario)


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


def test_in_process_sdk_project_root_defaults_tool_cwd(tmp_path: Path) -> None:
    project = tmp_path / "Wisp-344"
    project.mkdir()
    target = project / "notes.txt"
    target.write_text("selected worktree\n", encoding="utf-8")

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path / "sessions"),
            options=InProcessOptions(
                allow_read_tools=True,
                startup_trusted=True,
                project_context_root=project,
            ),
        )
        try:
            context = controller._in_process_transport._host.agent.tool_context
            result = await ReadTool().run({"path": str(target)}, context)

            assert context.cwd == project.resolve(strict=False)
            assert result.text == "selected worktree\n"
        finally:
            await controller.aclose()

    anyio.run(scenario)


def test_in_process_sdk_explicit_cwd_preserves_project_subdirectory(tmp_path: Path) -> None:
    project = tmp_path / "project"
    working_directory = project / "src"
    working_directory.mkdir(parents=True)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path / "sessions"),
            options=InProcessOptions(
                startup_trusted=True,
                project_context_root=project,
                cwd=working_directory,
            ),
        )
        try:
            host = controller._in_process_transport._host
            assert host.agent.tool_context.cwd == working_directory.resolve(strict=False)
            assert host.agent.project_context_root == project.resolve(strict=False)
        finally:
            await controller.aclose()

    anyio.run(scenario)


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


def test_in_process_sdk_recovers_after_unexpected_prompt_exception(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        host = controller._in_process_transport._host

        def fail_run(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("unexpected SDK prompt failure")

        monkeypatch.setattr(host.agent, "run", fail_run)
        prompt_id = await controller.prompt("hello", command_id="prompt-1")
        state_id: str | None = None
        events = []
        try:
            async for event in controller.events():
                events.append(event)
                if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                    state_id = await controller.get_state(command_id="state-1")
                elif isinstance(event, RpcCommandFinished) and event.command_id == state_id:
                    break
        finally:
            await controller.aclose()

        terminals = [event for event in events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_id, event.ok, event.error) for event in terminals] == [
            ("prompt-1", False, "unexpected SDK prompt failure"),
            ("state-1", True, None),
        ]
        assert any(
            isinstance(event, RpcStateReported) and event.command_id == "state-1"
            for event in events
        )

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


def test_in_process_sdk_preserves_typed_secret_command_until_storage(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        secret = "sentinel-secret-key"
        auth_path = tmp_path / "auth.json"
        controller = await InProcessWisp.start(
            WispConfig(
                provider="fake",
                session_dir=tmp_path / "sessions",
                auth_path=auth_path,
            ),
            options=InProcessOptions(startup_trusted=True),
        )
        transport = controller._in_process_transport
        original_control_send = transport._control_send
        captured: list[_RpcInputCommand] = []

        class InspectingSend:
            async def send(self, event: _RpcInputCommand | _RpcInputClosed) -> None:
                if isinstance(event, _RpcInputCommand):
                    captured.append(event)
                await original_control_send.send(event)

            async def aclose(self) -> None:
                await original_control_send.aclose()

        transport._control_send = cast(Any, InspectingSend())

        try:

            def fail_legacy(*_args: object, **_kwargs: object) -> None:
                pytest.fail("SDK credential storage must use typed execution")

            with monkeypatch.context() as patch:
                patch.setattr(rpc_execution_module.RpcCommandExecutor, "dispatch", fail_legacy)
                patch.setattr(ParsedRpcCommand, "to_legacy_dict", fail_legacy)
                command_id = await controller.store_api_key(
                    "anthropic",
                    secret,
                    command_id="store-1",
                )
                events = []
                async for event in controller.events():
                    events.append(event)
                    if isinstance(event, RpcCommandFinished) and event.command_id == command_id:
                        break
        finally:
            await controller.aclose()

        assert len(captured) == 1
        parsed = captured[0].command
        assert isinstance(parsed.known, StoreApiKeyCommand)
        assert parsed.known.api_key == secret
        assert secret not in repr(captured[0])
        assert secret not in repr(parsed)
        assert secret not in repr(parsed.to_legacy_dict())
        assert JsonAuthStore(auth_path).get("anthropic") == ApiKeyCredential(key=secret)
        assert all(secret not in repr(event) for event in events)
        assert isinstance(events[-1], RpcCommandFinished)
        assert events[-1].ok is True

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


@pytest.mark.slow
def test_in_process_sdk_shutdown_cancels_prompt_final_state_refresh(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    started = Event()
    release = Event()
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="scripted"),
                ProviderTextDelta(delta="hello"),
                ProviderResponseCompleted(content="hello"),
            ]
        ]
    )
    original_updated_state = rpc_execution_module.updated_rpc_session_state

    async def build_scripted_runtime(_config: WispConfig) -> WispRuntime:
        runtime = await build_runtime()
        runtime.providers.register(provider)
        return runtime

    def blocked_updated_state(
        session: JsonlSession,
        committed_history: tuple[Message, ...],
        entry_start: int,
    ) -> tuple[int, tuple[Message, ...]]:
        started.set()
        release.wait(timeout=5)
        return original_updated_state(session, committed_history, entry_start)

    monkeypatch.setattr(sdk_module, "build_runtime_for_config", build_scripted_runtime)
    monkeypatch.setattr(rpc_execution_module, "updated_rpc_session_state", blocked_updated_state)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="scripted", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        await controller.prompt("hello", command_id="prompt-1")
        try:
            with anyio.fail_after(1):
                while not started.is_set():
                    await anyio.sleep(0.01)
            with anyio.fail_after(1):
                await controller.aclose()
        finally:
            release.set()

    anyio.run(scenario)


def test_in_process_sdk_reports_prompt_final_state_refresh_errors(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="scripted"),
                ProviderTextDelta(delta="hello"),
                ProviderResponseCompleted(content="hello"),
            ]
        ]
    )

    async def build_scripted_runtime(_config: WispConfig) -> WispRuntime:
        runtime = await build_runtime()
        runtime.providers.register(provider)
        return runtime

    def failed_updated_state(
        _session: JsonlSession,
        _committed_history: tuple[Message, ...],
        _entry_start: int,
    ) -> tuple[int, tuple[Message, ...]]:
        raise OSError("session state unavailable")

    monkeypatch.setattr(sdk_module, "build_runtime_for_config", build_scripted_runtime)
    monkeypatch.setattr(rpc_execution_module, "updated_rpc_session_state", failed_updated_state)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="scripted", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        prompt_id = await controller.prompt("hello", command_id="prompt-1")
        events = []
        try:
            async for event in controller.events():
                events.append(event)
                if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                    break
        finally:
            await controller.aclose()

        finished = next(
            event
            for event in events
            if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id
        )
        assert finished.ok is False
        assert finished.error == "session state unavailable"

    anyio.run(scenario)


@pytest.mark.slow
def test_in_process_sdk_shutdown_cancels_compact_final_state_refresh(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    started = Event()
    release = Event()
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="scripted"),
                ProviderTextDelta(delta="first answer"),
                ProviderResponseCompleted(content="first answer"),
            ],
            [
                ProviderResponseStarted(model="scripted"),
                ProviderTextDelta(delta="second answer"),
                ProviderResponseCompleted(content="second answer"),
            ],
            [
                ProviderResponseStarted(model="scripted"),
                ProviderTextDelta(
                    delta=(
                        "## Goal\n"
                        "Preserve the completed work.\n"
                        "## Constraints & Preferences\n"
                        "Keep the existing behavior.\n"
                        "## Progress\n"
                        "### Done\n"
                        "Completed two turns.\n"
                        "### In Progress\n"
                        "None.\n"
                        "### Blocked\n"
                        "None.\n"
                        "## Already Investigated\n"
                        "Reviewed the prior transcript.\n"
                        "## Key Decisions\n"
                        "Use a durable checkpoint.\n"
                        "## Next Steps\n"
                        "Continue from the checkpoint.\n"
                        "## Critical Context\n"
                        "The session contains two completed turns."
                    )
                ),
                ProviderResponseCompleted(
                    content=(
                        "## Goal\n"
                        "Preserve the completed work.\n"
                        "## Constraints & Preferences\n"
                        "Keep the existing behavior.\n"
                        "## Progress\n"
                        "### Done\n"
                        "Completed two turns.\n"
                        "### In Progress\n"
                        "None.\n"
                        "### Blocked\n"
                        "None.\n"
                        "## Already Investigated\n"
                        "Reviewed the prior transcript.\n"
                        "## Key Decisions\n"
                        "Use a durable checkpoint.\n"
                        "## Next Steps\n"
                        "Continue from the checkpoint.\n"
                        "## Critical Context\n"
                        "The session contains two completed turns."
                    )
                ),
            ],
        ]
    )
    original_updated_state = rpc_execution_module.updated_rpc_session_state

    async def build_scripted_runtime(_config: WispConfig) -> WispRuntime:
        runtime = await build_runtime()
        runtime.providers.register(provider)
        return runtime

    def blocked_updated_state(
        session: JsonlSession,
        committed_history: tuple[Message, ...],
        entry_start: int,
    ) -> tuple[int, tuple[Message, ...]]:
        started.set()
        release.wait(timeout=5)
        return original_updated_state(session, committed_history, entry_start)

    monkeypatch.setattr(sdk_module, "build_runtime_for_config", build_scripted_runtime)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="scripted", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        completed_ids: set[str] = set()

        async def consume_events() -> None:
            async for event in controller.events():
                if isinstance(event, RpcCommandFinished):
                    completed_ids.add(event.command_id)

        async def wait_for_completion(command_id: str) -> None:
            with anyio.fail_after(1):
                while command_id not in completed_ids:
                    await anyio.sleep(0.01)

        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(consume_events)
                await controller.prompt("first", command_id="prompt-1")
                await wait_for_completion("prompt-1")
                await controller.prompt("second", command_id="prompt-2")
                await wait_for_completion("prompt-2")

                monkeypatch.setattr(
                    rpc_execution_module,
                    "updated_rpc_session_state",
                    blocked_updated_state,
                )
                await controller.compact(command_id="compact-1")

                with anyio.fail_after(1):
                    while not started.is_set():
                        await anyio.sleep(0.01)
                with anyio.fail_after(1):
                    await controller.aclose()
        finally:
            release.set()

    anyio.run(scenario)


def test_in_process_sdk_shutdown_cancels_clone_precommit_read(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    read_started = Event()
    release_read = Event()
    original_read_active_leaf_id = JsonlSession.read_active_leaf_id

    def blocked_read_active_leaf_id(session: JsonlSession) -> str | None:
        read_started.set()
        release_read.wait(timeout=5)
        return original_read_active_leaf_id(session)

    monkeypatch.setattr(JsonlSession, "read_active_leaf_id", blocked_read_active_leaf_id)
    monkeypatch.setattr(sdk_module, "_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        prompt_id = await controller.prompt("seed", command_id="prompt-1")
        try:
            async for event in controller.events():
                if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                    await controller.clone_session(command_id="clone-1")
                    break
            with anyio.fail_after(1):
                while not read_started.is_set():
                    await anyio.sleep(0.01)
            with anyio.fail_after(0.5):
                await controller.aclose()
        finally:
            release_read.set()

    anyio.run(scenario)


def test_in_process_sdk_shutdown_cancels_fork_precommit_read(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    read_started = Event()
    release_read = Event()
    original_read_active_leaf_id = JsonlSession.read_active_leaf_id

    def blocked_read_active_leaf_id(session: JsonlSession) -> str | None:
        read_started.set()
        release_read.wait(timeout=5)
        return original_read_active_leaf_id(session)

    monkeypatch.setattr(JsonlSession, "read_active_leaf_id", blocked_read_active_leaf_id)
    monkeypatch.setattr(sdk_module, "_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        prompt_id = await controller.prompt("seed", command_id="prompt-1")
        try:
            async for event in controller.events():
                if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                    session_state = controller._in_process_transport._host.coordinator.session_state
                    source = session_state.session
                    assert source is not None
                    entry = next(
                        entry
                        for entry in source.read_entries()
                        if isinstance(entry, MessageSessionEntry) and entry.message.role == "user"
                    )
                    await controller.fork_session(entry.id, command_id="fork-1")
                    break
            with anyio.fail_after(1):
                while not read_started.is_set():
                    await anyio.sleep(0.01)
            with anyio.fail_after(0.5):
                await controller.aclose()
        finally:
            release_read.set()

    anyio.run(scenario)


def test_in_process_sdk_shutdown_cancels_navigation_precommit_read(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    read_started = Event()
    release_read = Event()
    original_read_active_leaf_id = JsonlSession.read_active_leaf_id

    def blocked_read_active_leaf_id(session: JsonlSession) -> str | None:
        read_started.set()
        release_read.wait(timeout=5)
        return original_read_active_leaf_id(session)

    monkeypatch.setattr(JsonlSession, "read_active_leaf_id", blocked_read_active_leaf_id)
    monkeypatch.setattr(sdk_module, "_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        prompt_id = await controller.prompt("seed", command_id="prompt-1")
        try:
            async for event in controller.events():
                if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                    session_state = controller._in_process_transport._host.coordinator.session_state
                    source = session_state.session
                    assert source is not None
                    entry = next(
                        entry
                        for entry in source.read_entries()
                        if isinstance(entry, MessageSessionEntry) and entry.message.role == "user"
                    )
                    await controller.navigate_session_tree(entry.id, command_id="navigate-1")
                    break
            with anyio.fail_after(1):
                while not read_started.is_set():
                    await anyio.sleep(0.01)
            with anyio.fail_after(0.5):
                await controller.aclose()
        finally:
            release_read.set()

    anyio.run(scenario)


def test_in_process_sdk_shutdown_cancels_unrevert_precommit_read(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    read_started = Event()
    release_read = Event()
    original_read_active_leaf_id = JsonlSession.read_active_leaf_id

    def blocked_read_active_leaf_id(session: JsonlSession) -> str | None:
        read_started.set()
        release_read.wait(timeout=5)
        return original_read_active_leaf_id(session)

    monkeypatch.setattr(sdk_module, "_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        prompt_id = await controller.prompt("seed", command_id="prompt-1")
        navigation_id: str | None = None
        try:
            async for event in controller.events():
                if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                    session_state = controller._in_process_transport._host.coordinator.session_state
                    source = session_state.session
                    assert source is not None
                    entry = next(
                        entry
                        for entry in source.read_entries()
                        if isinstance(entry, MessageSessionEntry) and entry.message.role == "user"
                    )
                    navigation_id = await controller.navigate_session_tree(
                        entry.id,
                        command_id="navigate-1",
                    )
                elif (
                    navigation_id is not None
                    and isinstance(event, RpcCommandFinished)
                    and event.command_id == navigation_id
                ):
                    monkeypatch.setattr(
                        JsonlSession,
                        "read_active_leaf_id",
                        blocked_read_active_leaf_id,
                    )
                    await controller.unrevert_session_tree(command_id="unrevert-1")
                    break
            with anyio.fail_after(1):
                while not read_started.is_set():
                    await anyio.sleep(0.01)
            with anyio.fail_after(0.5):
                await controller.aclose()
        finally:
            release_read.set()

    anyio.run(scenario)


def test_in_process_sdk_shutdown_cancels_session_name_load(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    load_started = Event()
    release_load = Event()
    original_load = JsonlSessionStore.load

    def blocked_load(store: JsonlSessionStore, reference: str | Path) -> JsonlSession:
        load_started.set()
        release_load.wait(timeout=5)
        return original_load(store, reference)

    monkeypatch.setattr(sdk_module, "_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        prompt_id = await controller.prompt("seed", command_id="prompt-1")
        try:
            async for event in controller.events():
                if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                    session_state = controller._in_process_transport._host.coordinator.session_state
                    source = session_state.session
                    assert source is not None
                    monkeypatch.setattr(JsonlSessionStore, "load", blocked_load)
                    await controller.set_session_name(
                        "renamed",
                        session_id=source.session_id,
                        command_id="rename-1",
                    )
                    break
            with anyio.fail_after(1):
                while not load_started.is_set():
                    await anyio.sleep(0.01)
            with anyio.fail_after(0.5):
                await controller.aclose()
        finally:
            release_load.set()

    anyio.run(scenario)


@pytest.mark.slow
def test_in_process_sdk_shutdown_cancels_prompt_start_snapshot(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    started = Event()
    release = Event()
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="scripted"),
                ProviderTextDelta(delta="hello"),
                ProviderResponseCompleted(content="hello"),
            ]
        ]
    )
    original_run_start = rpc_execution_module.rpc_session_run_start

    async def build_scripted_runtime(_config: WispConfig) -> WispRuntime:
        runtime = await build_runtime()
        runtime.providers.register(provider)
        return runtime

    def blocked_run_start(session: JsonlSession, entry_start: int) -> tuple[int, str | None]:
        started.set()
        release.wait(timeout=5)
        return original_run_start(session, entry_start)

    monkeypatch.setattr(sdk_module, "build_runtime_for_config", build_scripted_runtime)
    monkeypatch.setattr(rpc_execution_module, "rpc_session_run_start", blocked_run_start)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="scripted", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        await controller.prompt("hello", command_id="prompt-1")
        try:
            with anyio.fail_after(1):
                while not started.is_set():
                    await anyio.sleep(0.01)
            with anyio.fail_after(1):
                await controller.aclose()
        finally:
            release.set()

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


@pytest.mark.production_fault
def test_in_process_sdk_shutdown_cancels_backpressured_stream(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="scripted"),
                *(
                    ProviderTextDelta(delta="x")
                    for _ in range(sdk_module._STREAM_EVENT_BUFFER_CAPACITY + 10)
                ),
                ProviderResponseCompleted(content="done"),
            ]
        ]
    )

    async def build_scripted_runtime(_config: WispConfig) -> WispRuntime:
        runtime = await build_runtime()
        runtime.providers.register(provider)
        return runtime

    monkeypatch.setattr(sdk_module, "build_runtime_for_config", build_scripted_runtime)
    monkeypatch.setattr(sdk_module, "_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="scripted", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        await controller.prompt("burst", command_id="prompt-1")

        event_output = controller._in_process_transport._event_output
        with anyio.fail_after(1):
            while event_output._buffered_event_count() < sdk_module._STREAM_EVENT_BUFFER_CAPACITY:
                await anyio.sleep(0.01)
        with anyio.fail_after(0.5):
            await controller.aclose()

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
            agent=cast(Any, object()),
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
            agent=cast(Any, object()),
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


@pytest.fixture(autouse=True)
def _guard_typed_command_dispatch(monkeypatch: MonkeyPatch) -> None:
    original = rpc_execution_module.RpcCommandExecutor.dispatch

    async def guarded(self: object, command: dict[str, object], running: object) -> object:
        assert command.get("type") not in {
            "cancel",
            "approval",
            "trust",
            "shutdown",
            "prompt",
            "init",
            "compact",
            "get_session_stats",
        }, "Known commands must execute through typed handlers"
        return await original(self, command, running)

    monkeypatch.setattr(rpc_execution_module.RpcCommandExecutor, "dispatch", guarded)
