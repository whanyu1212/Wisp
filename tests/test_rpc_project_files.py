"""Project discovery wire, policy, and physical-worker lifecycle contracts."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event

import anyio
import pytest
from pydantic import ValidationError

from tests.rpc_support import build_rpc_executor_fixture
from wisp.events import (
    ProjectFilesInvalidated,
    RpcCommandFinished,
    RpcProjectFile,
    RpcProjectFilesReported,
    WispEvent,
)
from wisp.project_files import FileIndexConfig, collect_project_snapshot
from wisp.rpc import project_files as discovery
from wisp.rpc.commands import GetProjectFilesCommand, ParsedRpcCommand, RpcCommandAdapter
from wisp.rpc.coordinator import _RpcInputClosed, _RpcInputCommand
from wisp.rpc.host import RpcHost
from wisp.rpc.project_files import project_files_report
from wisp.tools.context import ToolContext


async def wait_finished(events: list[WispEvent], command_id: str) -> RpcCommandFinished:
    with anyio.fail_after(5):
        while True:
            for event in events:
                if isinstance(event, RpcCommandFinished) and event.command_id == command_id:
                    return event
            await anyio.sleep(0.001)


@asynccontextmanager
async def running_host(tmp_path: Path):  # type: ignore[no-untyped-def]
    fixture = await build_rpc_executor_fixture(tmp_path / "sessions")
    fixture.agent.reconfigure(
        replace(fixture.agent.configuration, tool_context=ToolContext(cwd=tmp_path))
    )
    host = RpcHost(
        runtime=fixture.runtime,
        sessions=fixture.sessions,
        agent=fixture.agent,
        approval_policy=fixture.approval_policy,
        trust_gate=fixture.trust_gate,
        configure_overrides=fixture.configure_overrides,
        coordinator=fixture.coordinator,
        write_event=fixture.writer,
        render_events=fixture.writer.render_events,
    )
    send, receive = anyio.create_memory_object_stream(100)
    finished = anyio.Event()

    async def serve() -> None:
        try:
            await host.run_with_streams(receive, send=send, task_group=group)
        finally:
            finished.set()

    async def command(**payload: object) -> None:
        await send.send(
            _RpcInputCommand(
                ParsedRpcCommand.from_known(RpcCommandAdapter.validate_python(payload))
            )
        )

    try:
        async with send, receive, anyio.create_task_group() as group:
            group.start_soon(serve)
            try:
                yield host, command, fixture.events
            finally:
                await send.send(_RpcInputClosed())
                with anyio.fail_after(5):
                    await finished.wait()
    finally:
        await fixture.runtime.aclose()


def test_discovery_contract_and_refresh(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "资料.py").touch()
    (tmp_path / ".env").touch()

    async def scenario() -> None:
        async with running_host(tmp_path) as (_, command, events):
            await command(type="get_project_files", id="files-1")
            assert (await wait_finished(events, "files-1")).ok
            report = next(e for e in events if isinstance(e, RpcProjectFilesReported))
            assert RpcProjectFile(path="src/资料.py", kind="file") in report.entries
            encoded = report.model_dump_json()
            assert str(tmp_path) not in encoded and ".env" not in encoded
            (tmp_path / "added.py").touch()
            await command(type="get_project_files", id="files-2")
            assert (await wait_finished(events, "files-2")).ok
            latest = [e for e in events if isinstance(e, RpcProjectFilesReported)][-1]
            assert any(e.path == "added.py" for e in latest.entries)
            assert latest.generation == report.generation

    anyio.run(scenario)
    with pytest.raises(ValidationError):
        GetProjectFilesCommand.model_validate({"type": "get_project_files", "root": "/"})


def test_byte_budget_includes_escaping_envelope_and_preserves_ancestors(tmp_path: Path) -> None:
    (tmp_path / "parent").mkdir()
    for i in range(12):
        (tmp_path / "parent" / f'资料"{i:02}.py').touch()
    snapshot = collect_project_snapshot(
        FileIndexConfig(root=tmp_path, context=ToolContext(cwd=tmp_path))
    )
    report = project_files_report(snapshot, command_id='"' * 256, generation=1, max_bytes=850)
    assert len(report.model_dump_json().encode()) + 1 <= 850
    assert report.truncated and report.entries
    assert report.entries[0] == RpcProjectFile(path="parent", kind="directory")
    assert all(
        entry.path == "parent" or entry.path.startswith("parent/") for entry in report.entries
    )


def test_blocked_scan_allows_prompts_and_cancel_without_admitting_more_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release = Event(), Event()
    original = discovery.collect_project_snapshot

    def blocked(config: FileIndexConfig, **kwargs: object):  # type: ignore[no-untyped-def]
        started.set()
        assert release.wait(5)
        return original(config, **kwargs)

    monkeypatch.setattr(discovery, "collect_project_snapshot", blocked)

    async def scenario() -> None:
        async with running_host(tmp_path) as (_, command, events):
            try:
                await command(type="get_project_files", id="files")
                assert await anyio.to_thread.run_sync(started.wait, 3)
                await command(type="prompt", id="prompt", prompt="hello")
                assert (await wait_finished(events, "prompt")).ok
                await command(type="get_state", id="state")
                assert (await wait_finished(events, "state")).ok
                await command(type="cancel", id="cancel", target_id="files")
                assert (await wait_finished(events, "cancel")).ok
                assert not (await wait_finished(events, "files")).ok
                await command(type="get_project_files", id="busy")
                assert "busy" in (await wait_finished(events, "busy")).error
            finally:
                release.set()
        assert not any(isinstance(e, RpcProjectFilesReported) for e in events)
        assert (
            sum(isinstance(e, RpcCommandFinished) and e.command_id == "files" for e in events) == 1
        )

    anyio.run(scenario)


def test_policy_waiter_resumes_with_reserved_credentials(tmp_path: Path) -> None:
    (tmp_path / "new-credentials").touch()
    (tmp_path / "visible").touch()

    async def scenario() -> None:
        async with running_host(tmp_path) as (host, command, events):
            invalidated = host.project_files.begin_policy_transition()
            host.project_files.reserve_protected_paths((str(tmp_path / "new-credentials"),))
            await command(type="get_project_files", id="waiting")
            await command(type="get_state", id="state")
            await wait_finished(events, "state")
            assert not any(isinstance(e, RpcProjectFilesReported) for e in events)
            # Simulate a failed adoption: the old agent policy remains active.
            host.project_files.settle_policy(host.agent.tool_context)
            assert (await wait_finished(events, "waiting")).ok
            report = next(e for e in events if isinstance(e, RpcProjectFilesReported))
            assert report.generation == invalidated.generation
            assert any(e.path == "visible" for e in report.entries)
            assert "new-credentials" not in report.model_dump_json()

    anyio.run(scenario)


def test_invalidation_rechecks_result_after_waiting_for_publication(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_host(tmp_path) as (host, _, events):
            snapshot = collect_project_snapshot(
                FileIndexConfig(root=tmp_path, context=host.agent.tool_context)
            )
            report = project_files_report(snapshot, command_id="old", generation=1)
            waiting = anyio.Event()
            published: list[bool] = []

            async def publish() -> None:
                waiting.set()
                published.append(await host._publish_project_files(report, anyio.CancelScope()))

            async with anyio.create_task_group() as group:
                async with host._event_render_lock:
                    group.start_soon(publish)
                    await waiting.wait()
                    invalidated = host.project_files.begin_policy_transition()
                    events.append(invalidated)
                    host.project_files.settle_policy(host.agent.tool_context)
            assert published == [False]
            assert not any(isinstance(e, RpcProjectFilesReported) for e in events)

    anyio.run(scenario)


@pytest.mark.parametrize("shutdown", [False, True])
def test_close_cancels_discovery_waiting_for_policy(tmp_path: Path, shutdown: bool) -> None:
    async def scenario() -> None:
        async with running_host(tmp_path) as (host, command, events):
            host.project_files.begin_policy_transition()
            await command(type="get_project_files", id="waiting")
            await command(type="get_state", id="state")
            await wait_finished(events, "state")
            if shutdown:
                await command(type="shutdown", id="shutdown")
                assert (await wait_finished(events, "shutdown")).ok
        assert not (await wait_finished(events, "waiting")).ok
        assert not any(isinstance(e, RpcProjectFilesReported) for e in events)

    anyio.run(scenario)


def test_cross_language_project_files_fixture() -> None:
    fixtures = json.loads((Path(__file__).parent / "fixtures/rpc_project_files.json").read_text())
    assert RpcProjectFilesReported.model_validate(fixtures["report"]).entries
    assert ProjectFilesInvalidated.model_validate(fixtures["invalidated"]).generation == 2


def test_controller_uses_injected_command_id_factory() -> None:
    from tests.test_rpc import RecordingTransport
    from wisp.rpc.client import RpcController

    async def scenario() -> None:
        transport = RecordingTransport()
        controller = RpcController(transport, command_id_factory=lambda prefix: f"{prefix}-id")
        assert await controller.get_project_files() == "get_project_files-id"
        assert transport.commands == [GetProjectFilesCommand(id="get_project_files-id")]

    anyio.run(scenario)


@pytest.mark.parametrize("failure", ["exception", "cancel"])
def test_candidate_credentials_are_reserved_before_interrupted_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from wisp.auth.storage import JsonAuthStore
    from wisp.coding import CodingSession
    from wisp.config import WispConfig
    from wisp.rpc.configuration import RpcProjectConfiguration, _ConfigOverrides
    from wisp.rpc.project_files import RpcProjectFiles
    from wisp.runtime.api import WispRuntime
    from wisp.runtime.extensions import build_runtime
    from wisp.sessions.jsonl import JsonlSessionStore

    initial = WispConfig(
        provider="fake", auth_path=tmp_path / "old-auth", session_dir=tmp_path / "sessions"
    )
    candidate = WispConfig(
        provider="fake", auth_path=tmp_path / "new-auth", session_dir=initial.session_dir
    )
    # Exercise ToolContext's credential backstop even for a configuration copy
    # that did not rerun WispConfig validation.
    candidate = candidate.model_copy(update={"protected_paths": initial.protected_paths})
    monkeypatch.setattr(_ConfigOverrides, "build", lambda *_args, **_kwargs: candidate)
    (tmp_path / "old-auth").write_text("{}")
    (tmp_path / "new-auth").write_text("{}")
    original = WispRuntime.adopt_provider_configuration

    async def scenario() -> None:
        runtime = await build_runtime(auth_path=initial.auth_path)
        agent = CodingSession(
            provider=runtime.providers.get("fake"),
            sessions=JsonlSessionStore(initial.session_dir),
            tool_context=ToolContext.from_config(initial, cwd=tmp_path),
        )
        files = RpcProjectFiles(agent.tool_context)
        files.begin_policy_transition()
        scope = anyio.CancelScope()

        async def interrupted_adoption(self: WispRuntime, adopted: WispRuntime) -> None:
            assert str(candidate.auth_path) in files._reserved_paths
            await original(self, adopted)
            if failure == "cancel":
                scope.cancel()
                await anyio.lowlevel.checkpoint()
            raise RuntimeError("cleanup interrupted")

        monkeypatch.setattr(WispRuntime, "adopt_provider_configuration", interrupted_adoption)

        async def builder(config):  # type: ignore[no-untyped-def]
            return await build_runtime(auth_path=config.auth_path)

        transition = RpcProjectConfiguration(
            startup_config=initial,
            startup_trusted=False,
            config_overrides=_ConfigOverrides(),
            project_context_root=tmp_path,
            runtime_builder=builder,
        )
        try:
            with scope:
                try:
                    await transition.apply_trusted_project(
                        runtime=runtime,
                        agent=agent,
                        reserve_protected_paths=files.reserve_protected_paths,
                    )
                except RuntimeError:
                    assert failure == "exception"
                finally:
                    files.settle_policy(agent.tool_context)
            assert isinstance(runtime.auth_store, JsonAuthStore)
            assert runtime.auth_store.path == candidate.auth_path
            assert str(candidate.auth_path) not in agent.tool_context.protected_paths
            snapshot = collect_project_snapshot(
                FileIndexConfig(root=tmp_path, context=files._context)
            )
            assert "old-auth" not in snapshot.paths and "new-auth" not in snapshot.paths
        finally:
            await runtime.aclose()

    anyio.run(scenario)


def test_host_trust_transition_invalidates_even_when_configuration_is_equal(tmp_path: Path) -> None:
    from tests.rpc_support import RecordingEventWriter
    from wisp.config import WispConfig
    from wisp.rpc.host import InProcessOptions
    from wisp.runtime.extensions import build_runtime

    async def scenario() -> None:
        config = WispConfig(provider="fake", session_dir=tmp_path / "sessions")
        runtime = await build_runtime(auth_path=config.auth_path)
        writer = RecordingEventWriter()
        try:
            host = await RpcHost.create(
                config,
                runtime,
                options=InProcessOptions(cwd=tmp_path, startup_trusted=False),
                write_event=writer,
                render_events=writer.render_events,
            )
            assert await host.trust_gate._finish(True)
            invalidations = [e for e in writer.events if isinstance(e, ProjectFilesInvalidated)]
            assert len(invalidations) == 1
            assert host.project_files.is_current(invalidations[0].generation)
        finally:
            await runtime.aclose()

    anyio.run(scenario)


@pytest.mark.parametrize("exit_kind", ["timeout", "eof", "shutdown"])
def test_blocked_physical_scan_does_not_hold_request_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_kind: str
) -> None:
    started, release, exited = Event(), Event(), Event()
    original = discovery.collect_project_snapshot

    def blocked(config: FileIndexConfig, **kwargs: object):  # type: ignore[no-untyped-def]
        started.set()
        try:
            assert release.wait(5)
            return original(config, **kwargs)
        finally:
            exited.set()

    monkeypatch.setattr(discovery, "collect_project_snapshot", blocked)
    if exit_kind == "timeout":
        monkeypatch.setattr(
            discovery,
            "FileIndexConfig",
            lambda **kwargs: replace(FileIndexConfig(**kwargs), timeout_seconds=0.1),
        )

    async def scenario() -> None:
        try:
            async with running_host(tmp_path) as (_, command, events):
                await command(type="get_project_files", id="blocked")
                assert await anyio.to_thread.run_sync(started.wait, 3)
                if exit_kind == "timeout":
                    assert "timed out" in (await wait_finished(events, "blocked")).error
                    await command(type="get_project_files", id="busy")
                    assert "busy" in (await wait_finished(events, "busy")).error
                elif exit_kind == "shutdown":
                    await command(type="shutdown", id="shutdown")
                    assert (await wait_finished(events, "shutdown")).ok
            assert not exited.is_set()
            assert not (await wait_finished(events, "blocked")).ok
        finally:
            release.set()
            assert await anyio.to_thread.run_sync(exited.wait, 3)
        assert not any(isinstance(e, RpcProjectFilesReported) for e in events)

    anyio.run(scenario)
