"""Idle reload candidates, precedence, rollback, and RPC round trips."""

from __future__ import annotations

import json
import shlex
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError

from wisp.coding import CodingSession, resolve_coding_session_configuration
from wisp.config.runtime import WispConfig
from wisp.config.settings import SettingsDocumentError, resolve_settings
from wisp.events import (
    RpcCommandFinished,
    RpcSettingsReloaded,
    RpcStateReported,
    WispEvent,
    wisp_event_from_json,
)
from wisp.mcp.config import McpServerConfig
from wisp.rpc.configuration import RpcProjectConfiguration, _ConfigOverrides
from wisp.rpc.handlers.reload import RpcSettingsReloader
from wisp.runtime.api import WispRuntime
from wisp.runtime.extensions import build_runtime
from wisp.runtime.registry import UnknownProviderError
from wisp.sdk import InProcessOptions, InProcessWisp
from wisp.sessions.jsonl import JsonlSessionStore


@dataclass
class ReloadFixture:
    agent: CodingSession
    runtime: WispRuntime
    reloader: RpcSettingsReloader
    user_file: Path
    project_file: Path

    def save(self, **values: object) -> None:
        self.user_file.write_text(json.dumps({"provider": "fake", **values}), encoding="utf-8")


@asynccontextmanager
async def session(root: Path, *, trusted: bool = True) -> AsyncIterator[ReloadFixture]:
    user_file = Path.home() / ".wisp/settings.json"
    user_file.parent.mkdir(parents=True, exist_ok=True)
    user_file.write_text('{"provider":"fake", "model":"before"}', encoding="utf-8")
    project_file = root / ".wisp/settings.json"
    project_file.parent.mkdir(parents=True, exist_ok=True)
    overrides = _ConfigOverrides(session_dir=root / "sessions")
    config = overrides.build(project_dir=root, trusted=trusted)
    runtime = await build_runtime(auth_path=config.auth_path, retry_policy=config.retry_policy)
    agent = CodingSession(
        provider=runtime.providers.get("fake"),
        sessions=JsonlSessionStore(config.session_dir),
    )
    agent.reconfigure(
        resolve_coding_session_configuration(
            config,
            providers=runtime.providers,
            models=runtime.models,
            trusted=trusted,
            cwd=root,
            project_root=root,
        )
    )
    project = RpcProjectConfiguration(
        startup_config=config,
        startup_trusted=trusted,
        config_overrides=overrides,
        project_context_root=root,
        runtime_builder=lambda _: _unused_builder(),
    )
    try:
        yield ReloadFixture(
            agent, runtime, RpcSettingsReloader(project, {}), user_file, project_file
        )
    finally:
        await runtime.aclose()


async def _unused_builder() -> WispRuntime:
    raise AssertionError("reload must never rebuild providers or MCP connections")


def test_reload_applies_live_fields_and_keeps_tools_paths_and_trust(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with session(tmp_path) as fixture:
            agent = fixture.agent
            context = agent.tool_context
            tools = agent.tools
            fixture.save(
                model="after",
                context_reserve_tokens=4096,
                auto_compaction_enabled=False,
                web_search=True,
                codemode=True,
                codemode_presentation="codemode_only",
                subagents=True,
            )
            report = await fixture.reloader.reload(
                agent=agent, runtime=fixture.runtime, command_id="r"
            )
            assert agent.model == "after"
            assert agent.context_reserve_tokens == 4096
            assert not agent.auto_compaction_enabled
            assert agent.web_search and agent.codemode and agent.subagents
            assert agent.codemode_presentation == "codemode_only"
            assert agent.tool_context is context and agent.tools is tools
            assert agent.trusted
            assert set(report.applied) == {
                "model",
                "context_reserve_tokens",
                "auto_compaction_enabled",
                "web_search",
                "codemode",
                "codemode_presentation",
                "subagents",
            }
            assert report.state.model == "after" and report.state.web_search
            assert wisp_event_from_json(report.model_dump_json()) == report
            again = await fixture.reloader.reload(
                agent=agent, runtime=fixture.runtime, command_id="again"
            )
            assert not again.applied and not again.restart_required

    anyio.run(scenario)


@pytest.mark.parametrize(
    "raw",
    [
        '{"web_search":',
        "[]",
        '{"web_search":"SECRET_DO_NOT_PRINT"}',
        '{"tui_keybindings":7}',
        '{"mcp_servers":{"secret":{"command":7,"env":{"TOKEN":"SECRET_DO_NOT_PRINT"}}}}',
    ],
)
def test_invalid_reload_preserves_configuration_and_redacts_values(
    tmp_path: Path, raw: str
) -> None:
    async def scenario() -> None:
        async with session(tmp_path) as fixture:
            before = fixture.agent.configuration
            active = fixture.reloader.project.active_config
            fixture.user_file.write_text(raw, encoding="utf-8")
            with pytest.raises(SettingsDocumentError) as error:
                await fixture.reloader.reload(
                    agent=fixture.agent, runtime=fixture.runtime, command_id="r"
                )
            assert "SECRET_DO_NOT_PRINT" not in str(error.value)
            assert fixture.agent.configuration == before
            assert fixture.reloader.project.active_config is active

    anyio.run(scenario)


def test_restart_only_settings_are_reported_but_never_adopted(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with session(tmp_path) as fixture:
            context = fixture.agent.tool_context
            config = fixture.reloader.project.active_config
            fixture.save(
                model="after",
                auth_path=str(tmp_path / "new-auth"),
                protected_paths=[],
                session_dir=str(tmp_path / "other-sessions"),
                wake_on_process_exit=True,
                codex_read_timeout_seconds=11,
                mcp_servers={"demo": {"command": "must-not-execute"}},
                tui_keybindings={"submit": ["Ctrl+Enter"]},
            )
            report = await fixture.reloader.reload(
                agent=fixture.agent, runtime=fixture.runtime, command_id="r"
            )
            assert {
                "auth_path",
                "protected_paths",
                "wake_on_process_exit",
                "codex_read_timeout_seconds",
                "mcp_servers",
                "tui_keybindings",
            } <= set(report.restart_required)
            assert fixture.agent.model == "after" and fixture.agent.tool_context is context
            assert "session_dir" not in report.restart_required  # Explicit CLI override wins.
            assert fixture.reloader.project.active_config.auth_path == config.auth_path
            assert fixture.reloader.project.active_config.mcp_servers == ()
            assert "must-not-execute" not in report.model_dump_json()

    anyio.run(scenario)


def test_session_choices_survive_reload(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with session(tmp_path) as fixture:
            choices = fixture.reloader.project.configure_overrides
            choices.model = "chosen"
            choices.has_model = True
            choices.web_search = True
            choices.auto_compaction_enabled = False
            choices.has_auto_compaction_enabled = True
            choices.codemode = None
            choices.has_codemode = True
            choices.codemode_presentation = "codemode_only"
            choices.subagents = False
            choices.has_subagent_model = True
            fixture.save(
                provider="unregistered-provider",
                model="file-model",
                web_search=False,
                auto_compaction_enabled=True,
                codemode=True,
                subagents=True,
                subagent_model="file-model",
            )
            report = await fixture.reloader.reload(
                agent=fixture.agent, runtime=fixture.runtime, command_id="r"
            )
            assert fixture.agent.provider.name == "fake" and fixture.agent.model == "chosen"
            assert fixture.agent.web_search and not fixture.agent.auto_compaction_enabled
            assert fixture.agent.codemode is None and not fixture.agent.subagents
            assert fixture.agent.codemode_presentation == "codemode_only"
            assert fixture.agent.subagent_model is None
            assert {
                "provider",
                "model",
                "web_search",
                "auto_compaction_enabled",
                "codemode",
                "codemode_presentation",
                "subagents",
                "subagent_model",
            } <= set(report.overridden)

    anyio.run(scenario)


@pytest.mark.parametrize("trusted", [False, True])
def test_reload_uses_project_root_and_preserves_trust_filter(tmp_path: Path, trusted: bool) -> None:
    async def scenario() -> None:
        async with session(tmp_path, trusted=trusted) as fixture:
            fixture.project_file.write_text(
                json.dumps(
                    {
                        "model": "project-model",
                        "web_search": "invalid-user-only-field",
                        "protected_paths": [],
                    }
                ),
                encoding="utf-8",
            )
            report = await fixture.reloader.reload(
                agent=fixture.agent, runtime=fixture.runtime, command_id="r"
            )
            assert fixture.agent.model == ("project-model" if trusted else "before")
            assert not fixture.agent.web_search
            assert fixture.agent.trusted is trusted
            assert not report.restart_required

    anyio.run(scenario)


def test_environment_and_cli_precedence_survive_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async with session(tmp_path) as fixture:
            monkeypatch.setenv("WISP_MODEL", "environment-model")
            fixture.reloader.project.config_overrides = _ConfigOverrides(
                provider="fake", session_dir=tmp_path / "sessions"
            )
            fixture.save(provider="unregistered-provider", model="file-model")
            await fixture.reloader.reload(
                agent=fixture.agent, runtime=fixture.runtime, command_id="r"
            )
            assert (
                fixture.agent.provider.name == "fake" and fixture.agent.model == "environment-model"
            )

    anyio.run(scenario)


def test_missing_settings_restore_defaults_and_bad_provider_rolls_back(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with session(tmp_path) as fixture:
            fixture.reloader.project.config_overrides = _ConfigOverrides(
                provider="fake", session_dir=tmp_path / "sessions"
            )
            fixture.user_file.unlink()
            report = await fixture.reloader.reload(
                agent=fixture.agent, runtime=fixture.runtime, command_id="r"
            )
            assert fixture.agent.model is None
            assert report.state.model == fixture.agent.state_snapshot().model == "fake"
            assert resolve_settings(strict=True).model is None
            fixture.reloader.project.config_overrides = _ConfigOverrides(
                session_dir=tmp_path / "sessions"
            )
            before = fixture.agent.configuration
            fixture.save(provider="not-registered", web_search=True)
            with pytest.raises(UnknownProviderError):
                await fixture.reloader.reload(
                    agent=fixture.agent, runtime=fixture.runtime, command_id="bad"
                )
            assert fixture.agent.configuration == before

    anyio.run(scenario)


def test_busy_session_is_rejected_before_reading_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async with session(tmp_path) as fixture:
            fixture.user_file.write_text("not-json", encoding="utf-8")
            monkeypatch.setattr(fixture.agent, "_operation_active", True)
            with pytest.raises(RuntimeError, match="idle-only"):
                await fixture.reloader.reload(
                    agent=fixture.agent, runtime=fixture.runtime, command_id="r"
                )
            monkeypatch.setattr(fixture.agent, "_operation_active", False)

    anyio.run(scenario)


def test_reload_controller_round_trip_and_no_implicit_config_reload(tmp_path: Path) -> None:
    async def scenario() -> None:
        user_file = Path.home() / ".wisp/settings.json"
        user_file.parent.mkdir(parents=True, exist_ok=True)
        user_file.write_text('{"provider":"fake"}', encoding="utf-8")
        controller = await InProcessWisp.from_environment(
            provider="fake",
            session_dir=tmp_path / "sessions",
            options=InProcessOptions(cwd=tmp_path, project_context_root=tmp_path),
        )
        stream = controller.events()
        try:
            user_file.write_text('{"provider":"fake","web_search":true}', encoding="utf-8")
            command_id = await controller.reload(command_id="reload-test")
            events: list[WispEvent] = []
            with anyio.fail_after(10):
                async for event in stream:
                    events.append(event)
                    if isinstance(event, RpcCommandFinished) and event.command_id == command_id:
                        assert event.ok
                        break
            report = next(e for e in events if isinstance(e, RpcSettingsReloaded))
            assert report.applied == ("web_search",)
        finally:
            await controller.aclose()

    anyio.run(scenario)


def test_utf8_error_keeps_the_active_configuration(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with session(tmp_path) as fixture:
            before = fixture.agent.configuration
            fixture.user_file.write_bytes(b"\xff\xfe")
            with pytest.raises(SettingsDocumentError):
                await fixture.reloader.reload(
                    agent=fixture.agent, runtime=fixture.runtime, command_id="r"
                )
            assert fixture.agent.configuration == before

    anyio.run(scenario)


@pytest.mark.parametrize(
    "values", [{"model": "x" * 1024}, {"model": "after", "subagent_model": "x" * 1024}]
)
def test_unreportable_selection_fails_before_any_commit(
    tmp_path: Path, values: dict[str, str]
) -> None:

    async def scenario() -> None:
        async with session(tmp_path) as fixture:
            before = fixture.agent.configuration
            fixture.save(**values, web_search=True)
            with pytest.raises(ValidationError):
                await fixture.reloader.reload(
                    agent=fixture.agent, runtime=fixture.runtime, command_id="r"
                )
            assert fixture.agent.configuration == before

    anyio.run(scenario)


def test_startup_mcp_connection_is_not_idle(tmp_path: Path) -> None:

    async def scenario() -> None:
        async with session(tmp_path) as fixture:
            pending = replace(fixture.runtime, mcp_connecting=True)
            with pytest.raises(RuntimeError, match="idle-only"):
                await fixture.reloader.reload(agent=fixture.agent, runtime=pending, command_id="r")

    anyio.run(scenario)


@pytest.mark.parametrize("explicit_config", [False, True])
def test_rpc_reload_failure_is_sanitized_and_does_not_apply_settings(
    tmp_path: Path, explicit_config: bool
) -> None:

    async def scenario() -> None:
        user_file = Path.home() / ".wisp/settings.json"
        user_file.parent.mkdir(parents=True, exist_ok=True)
        user_file.write_text('{"provider":"fake"}', encoding="utf-8")
        options = InProcessOptions(cwd=tmp_path, project_context_root=tmp_path)
        if explicit_config:
            controller = await InProcessWisp.start(
                WispConfig(provider="fake", session_dir=tmp_path / "sessions"),
                options=options,
            )
        else:
            controller = await InProcessWisp.from_environment(
                provider="fake",
                session_dir=tmp_path / "sessions",
                options=options,
            )
        stream = controller.events()
        try:
            user_file.write_text('{"web_search":"SECRET_DO_NOT_PRINT"}', encoding="utf-8")
            command_id = await controller.reload()
            events: list[WispEvent] = []
            with anyio.fail_after(10):
                async for event in stream:
                    events.append(event)
                    if isinstance(event, RpcCommandFinished) and event.command_id == command_id:
                        assert not event.ok
                        break
            assert not any(isinstance(e, RpcSettingsReloaded) for e in events)
            assert all("SECRET_DO_NOT_PRINT" not in e.model_dump_json() for e in events)
            state_id = await controller.get_state()
            with anyio.fail_after(10):
                async for event in stream:
                    if isinstance(event, RpcStateReported) and event.command_id == state_id:
                        assert not event.state.web_search
                    if isinstance(event, RpcCommandFinished) and event.command_id == state_id:
                        assert event.ok
                        break
        finally:
            await controller.aclose()

    anyio.run(scenario)


def test_unavailable_subagent_model_does_not_partially_apply_other_fields(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with session(tmp_path) as fixture:
            before = fixture.agent.configuration
            fixture.save(model="after", web_search=True, subagent_model="not-in-the-fake-catalog")
            with pytest.raises(ValueError, match="Subagent model is unavailable"):
                await fixture.reloader.reload(
                    agent=fixture.agent, runtime=fixture.runtime, command_id="r"
                )
            assert fixture.agent.configuration == before

    anyio.run(scenario)


@pytest.mark.process
def test_idle_reload_preserves_a_running_background_process(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with session(tmp_path) as fixture:
            supervisor = fixture.runtime.process_supervisor
            code = shlex.quote("import time; time.sleep(60)")
            process_id = await supervisor.start(
                f"{shlex.quote(sys.executable)} -c {code}",
                cwd=tmp_path,
                timeout=10,
            )
            fixture.save(web_search=True)
            await fixture.reloader.reload(
                agent=fixture.agent, runtime=fixture.runtime, command_id="r"
            )
            assert (await supervisor.poll(process_id)).state == "running"
            assert fixture.agent.web_search

    anyio.run(scenario)


def test_reload_recognizes_mcp_settings_already_reconnected(tmp_path: Path) -> None:

    async def scenario() -> None:
        async with session(tmp_path) as fixture:
            server = McpServerConfig(name="demo", command="not-executed", enabled=False)
            fixture.save(mcp_servers={"demo": {"command": server.command, "enabled": False}})
            reconnected = replace(fixture.runtime, mcp_server_configs=(server,))
            report = await fixture.reloader.reload(
                agent=fixture.agent, runtime=reconnected, command_id="r"
            )
            assert "mcp_servers" not in report.restart_required

    anyio.run(scenario)


def test_project_refresh_cannot_hide_startup_only_changes(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with session(tmp_path) as fixture:
            fixture.reloader.project.config_overrides = _ConfigOverrides(provider="fake")
            changed_store = tmp_path / "other-sessions"
            fixture.save(session_dir=str(changed_store), wake_on_process_exit=True)
            # Moving between projects records the resolved config but deliberately
            # keeps the original session store and process-wake policy.
            fixture.reloader.project.active_config = (
                fixture.reloader.project.active_config.model_copy(
                    update={"session_dir": changed_store, "wake_on_process_exit": True}
                )
            )
            report = await fixture.reloader.reload(
                agent=fixture.agent, runtime=fixture.runtime, command_id="r"
            )
            assert {"session_dir", "wake_on_process_exit"} <= set(report.restart_required)

    anyio.run(scenario)


@pytest.mark.parametrize(
    ("choice", "variable", "value", "field"),
    [
        ("codemode", "WISP_CODEMODE", "1", "codemode"),
        (
            "presentation",
            "WISP_CODEMODE_PRESENTATION",
            "direct_and_codemode",
            "codemode_presentation",
        ),
        ("subagents", "WISP_SUBAGENTS", "1", "subagents"),
        ("subagent_model", "WISP_SUBAGENT_MODEL", "fake", "subagent_model"),
    ],
)
def test_explicit_rpc_choices_survive_reload_over_environment_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    choice: str,
    variable: str,
    value: str,
    field: str,
) -> None:
    from wisp.events import RpcCodemodeReported, RpcSubagentsReported

    monkeypatch.setenv(variable, value)

    async def scenario() -> None:
        user_file = Path.home() / ".wisp/settings.json"
        user_file.parent.mkdir(parents=True, exist_ok=True)
        user_file.write_text('{"provider":"fake"}', encoding="utf-8")
        controller = await InProcessWisp.from_environment(
            provider="fake",
            session_dir=tmp_path / "sessions",
            options=InProcessOptions(cwd=tmp_path, project_context_root=tmp_path),
        )
        stream = controller.events()

        async def finished(command_id: str) -> list[WispEvent]:
            events: list[WispEvent] = []
            with anyio.fail_after(10):
                while True:
                    event = await anext(stream)
                    events.append(event)
                    if isinstance(event, RpcCommandFinished) and event.command_id == command_id:
                        assert event.ok, event.error
                        return events

        try:
            if choice == "codemode":
                command_id = await controller.set_codemode("automatic")
            elif choice == "presentation":
                command_id = await controller.set_codemode(
                    "automatic", presentation="codemode_only"
                )
            elif choice == "subagents":
                command_id = await controller.set_subagents(False)
            else:
                command_id = await controller.set_subagent_model(None)
            await finished(command_id)
            events = await finished(await controller.reload())
            report = next(e for e in events if isinstance(e, RpcSettingsReloaded))
            assert field in report.overridden
            if choice in {"codemode", "presentation"}:
                events = await finished(await controller.get_codemode())
                snapshot = next(e for e in events if isinstance(e, RpcCodemodeReported))
                assert snapshot.codemode.setting == "automatic"
                if choice == "presentation":
                    assert snapshot.codemode.presentation == "codemode_only"
            else:
                events = await finished(await controller.get_subagents())
                snapshot = next(e for e in events if isinstance(e, RpcSubagentsReported))
                if choice == "subagents":
                    assert snapshot.subagents.enabled is False
                else:
                    assert snapshot.subagents.model is None
        finally:
            await controller.aclose()

    anyio.run(scenario)
