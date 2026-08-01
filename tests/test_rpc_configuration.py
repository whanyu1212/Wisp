from __future__ import annotations

import json
from pathlib import Path
from threading import Event, get_ident

import anyio
import pytest

from wisp.coding import CodingSession
from wisp.config import WispConfig
from wisp.rpc.configuration import RpcProjectConfiguration, _ConfigOverrides
from wisp.runtime.api import WispRuntime
from wisp.runtime.extensions import build_runtime
from wisp.runtime.registry import UnknownProviderError
from wisp.sessions.jsonl import JsonlSessionStore


async def _runtime_for(config: WispConfig) -> WispRuntime:
    return await build_runtime(auth_path=config.auth_path, retry_policy=config.retry_policy)


def test_no_settings_trust_transition_updates_session_without_rebuilding(
    tmp_path: Path,
) -> None:
    config = WispConfig(provider="fake", session_dir=tmp_path / "sessions")

    async def scenario() -> None:
        runtime = await _runtime_for(config)
        agent = CodingSession(
            provider=runtime.providers.get("fake"),
            sessions=JsonlSessionStore(config.session_dir),
            trusted=False,
        )

        async def unexpected_builder(_config: WispConfig) -> WispRuntime:
            raise AssertionError("identical trusted config must not rebuild the runtime")

        transition = RpcProjectConfiguration(
            startup_config=config,
            startup_trusted=False,
            config_overrides=_ConfigOverrides(
                provider="fake",
                session_dir=config.session_dir,
            ),
            project_context_root=tmp_path,
            runtime_builder=unexpected_builder,
        )

        event = await transition.apply_trusted_project(runtime=runtime, agent=agent)

        assert event is None
        assert agent.trusted is True

    anyio.run(scenario)


def test_trusted_project_transition_offloads_settings_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_thread = get_ident()
    build_threads: list[int] = []
    startup = WispConfig(provider="fake", session_dir=tmp_path / "sessions")
    original_build = _ConfigOverrides.build

    def build_in_worker(
        overrides: _ConfigOverrides,
        *,
        trusted: bool,
        project_dir: Path | None = None,
    ) -> WispConfig:
        assert trusted is True
        assert project_dir == tmp_path
        build_threads.append(get_ident())
        return original_build(overrides, trusted=trusted, project_dir=project_dir)

    monkeypatch.setattr(_ConfigOverrides, "build", build_in_worker)

    async def scenario() -> None:
        runtime = await _runtime_for(startup)
        agent = CodingSession(
            provider=runtime.providers.get("fake"),
            sessions=JsonlSessionStore(startup.session_dir),
            trusted=False,
        )
        transition = RpcProjectConfiguration(
            startup_config=startup,
            startup_trusted=False,
            config_overrides=_ConfigOverrides(provider="fake", session_dir=startup.session_dir),
            project_context_root=tmp_path,
            runtime_builder=_runtime_for,
        )

        event = await transition.apply_trusted_project(runtime=runtime, agent=agent)

        assert event is None
        assert agent.trusted is True
        await runtime.aclose()

    anyio.run(scenario)

    assert build_threads and all(thread_id != main_thread for thread_id in build_threads)


def test_trusted_project_transition_cancel_abandons_settings_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    startup = WispConfig(provider="fake", session_dir=tmp_path / "sessions")
    original_build = _ConfigOverrides.build

    async def scenario() -> None:
        started = Event()
        release = Event()
        cancelled = anyio.Event()

        def blocking_build(
            overrides: _ConfigOverrides,
            *,
            trusted: bool,
            project_dir: Path | None = None,
        ) -> WispConfig:
            started.set()
            release.wait(timeout=5)
            return original_build(overrides, trusted=trusted, project_dir=project_dir)

        monkeypatch.setattr(_ConfigOverrides, "build", blocking_build)

        runtime = await _runtime_for(startup)
        agent = CodingSession(
            provider=runtime.providers.get("fake"),
            sessions=JsonlSessionStore(startup.session_dir),
            trusted=False,
        )
        transition = RpcProjectConfiguration(
            startup_config=startup,
            startup_trusted=False,
            config_overrides=_ConfigOverrides(provider="fake", session_dir=startup.session_dir),
            project_context_root=tmp_path,
            runtime_builder=_runtime_for,
        )
        cancel_scope = anyio.CancelScope()

        async def apply_transition() -> None:
            with cancel_scope:
                try:
                    await transition.apply_trusted_project(runtime=runtime, agent=agent)
                except anyio.get_cancelled_exc_class():
                    cancelled.set()
                    raise
            if cancel_scope.cancel_called:
                cancelled.set()

        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(apply_transition)
                with anyio.fail_after(1):
                    while not started.is_set():
                        await anyio.sleep(0.01)
                cancel_scope.cancel()
                with anyio.fail_after(1):
                    await cancelled.wait()
        finally:
            release.set()
            await runtime.aclose()

    anyio.run(scenario)


def test_trusted_project_transition_applies_config_and_returns_event(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    project_auth = project / "project-auth.json"
    settings_dir = project / ".wisp"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        json.dumps(
            {
                "provider": "fake",
                "model": "project-model",
                "auth_path": str(project_auth),
            }
        ),
        encoding="utf-8",
    )
    session_dir = tmp_path / "sessions"
    startup = WispConfig.from_env(
        provider="fake",
        session_dir=session_dir,
        project_dir=project,
        trusted=False,
    )

    async def scenario() -> None:
        runtime = await _runtime_for(startup)
        events = runtime.events
        tools = runtime.tools
        providers = runtime.providers
        api = runtime.api
        agent = CodingSession(
            provider=runtime.providers.get("fake"),
            sessions=JsonlSessionStore(session_dir),
            events=events,
        )
        transition = RpcProjectConfiguration(
            startup_config=startup,
            startup_trusted=False,
            config_overrides=_ConfigOverrides(provider="fake", session_dir=session_dir),
            project_context_root=project,
            runtime_builder=_runtime_for,
        )

        event = await transition.apply_trusted_project(runtime=runtime, agent=agent)

        assert event is not None
        assert event.provider == "fake"
        assert event.model == "project-model"
        assert event.auth_path == project_auth
        assert agent.model == "project-model"
        assert agent.trusted is True
        assert project_auth.resolve().as_posix() in agent.tool_context.protected_paths
        assert runtime.events is events
        assert runtime.tools is tools
        assert runtime.providers is providers
        assert runtime.api is api
        assert agent.provider is runtime.providers.get("fake")

    anyio.run(scenario)


def test_trusted_project_transition_preserves_in_session_overrides(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".wisp").mkdir()
    (project / ".wisp" / "settings.json").write_text(
        json.dumps({"provider": "fake", "model": "project-model"}),
        encoding="utf-8",
    )
    startup = WispConfig.from_env(
        provider="fake",
        session_dir=tmp_path / "sessions",
        project_dir=project,
        trusted=False,
    )

    async def scenario() -> None:
        runtime = await _runtime_for(startup)
        agent = CodingSession(
            provider=runtime.providers.get("fake"),
            sessions=JsonlSessionStore(startup.session_dir),
        )
        transition = RpcProjectConfiguration(
            startup_config=startup,
            startup_trusted=False,
            config_overrides=_ConfigOverrides(provider="fake", session_dir=startup.session_dir),
            project_context_root=project,
            runtime_builder=_runtime_for,
        )
        transition.configure_overrides.model = "configured-model"
        transition.configure_overrides.has_model = True
        transition.configure_overrides.effort = "custom-tier"
        transition.configure_overrides.has_effort = True

        event = await transition.apply_trusted_project(runtime=runtime, agent=agent)

        assert event is not None
        assert event.model == "configured-model"
        assert event.effort == "custom-tier"
        assert agent.model == "configured-model"
        assert agent.effort == "custom-tier"

    anyio.run(scenario)


def test_failed_trusted_project_transition_leaves_live_runtime_and_agent_unchanged(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".wisp").mkdir()
    (project / ".wisp" / "settings.json").write_text(
        json.dumps({"provider": "missing-provider"}),
        encoding="utf-8",
    )
    startup = WispConfig.from_env(
        provider="fake",
        session_dir=tmp_path / "sessions",
        project_dir=project,
        trusted=False,
    )

    async def scenario() -> None:
        runtime = await _runtime_for(startup)
        provider = runtime.providers.get("fake")
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(startup.session_dir),
        )
        transition = RpcProjectConfiguration(
            startup_config=startup,
            startup_trusted=False,
            config_overrides=_ConfigOverrides(session_dir=startup.session_dir),
            project_context_root=project,
            runtime_builder=_runtime_for,
        )

        with pytest.raises(UnknownProviderError, match="missing-provider"):
            await transition.apply_trusted_project(runtime=runtime, agent=agent)

        assert runtime.providers.get("fake") is provider
        assert agent.provider is provider
        assert agent.trusted is False

    anyio.run(scenario)
