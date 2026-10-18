"""In-process SDK startup: offloaded setup, startup cancellation, and workspace options."""

from __future__ import annotations

from pathlib import Path
from threading import Event, get_ident
from typing import Any, cast

import anyio
import pytest
from pytest import MonkeyPatch

import wisp.sdk as sdk_module
from tests.support.rpc import guard_rpc_command_serialization
from wisp.config.runtime import WispConfig
from wisp.messages import Message
from wisp.rpc import host as rpc_host_module
from wisp.sdk import InProcessOptions, InProcessWisp
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore
from wisp.tools.files.operations import ReadTool


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


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)
