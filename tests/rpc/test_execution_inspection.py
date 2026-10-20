from __future__ import annotations

from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from tests.rpc.execution_support import (
    dispatch_parsed,
    parsed_command,
)
from tests.support.rpc import (
    build_rpc_executor_fixture,
    guard_rpc_command_serialization,
)
from wisp.coding import CodingSession
from wisp.events import (
    ErrorEvent,
    RpcCommandFinished,
    RpcCommandsReported,
    RpcCommandStarted,
    RpcConnectionCatalogReported,
    RpcMcpStatusReported,
    RpcModelCatalogReported,
    RpcSkillsReported,
    RpcStateReported,
    WispEvent,
)
from wisp.providers.catalog import ModelCatalog, ModelCatalogProviderEntry, ModelRegistry
from wisp.providers.fake import FakeProvider
from wisp.rpc.commands import (
    ConfigureCommand,
    GetModelCatalogCommand,
)
from wisp.rpc.configuration import _RpcConfigureOverrides
from wisp.rpc.coordinator import _RpcRunningCommand
from wisp.rpc.handlers import configure as rpc_configure_module
from wisp.rpc.handlers import inspection as rpc_inspection_module
from wisp.runtime.api import ExtensionAPI, WispRuntime
from wisp.runtime.commands import CommandArgument, CommandCategory, CommandDescriptor
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ProviderRegistry, ToolRegistry
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.skills.models import SkillCatalog, SkillDiagnostic, SkillEntry


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)


@pytest.mark.parametrize(
    ("command_type", "report_type"),
    [
        ("get_state", RpcStateReported),
        ("get_commands", RpcCommandsReported),
        ("get_model_catalog", RpcModelCatalogReported),
        ("get_connection_catalog", RpcConnectionCatalogReported),
        ("get_skills", RpcSkillsReported),
        ("get_mcp_status", RpcMcpStatusReported),
    ],
)
@pytest.mark.parametrize("id_fields", [{}, {"id": None}, {"id": "inspection-1"}])
@pytest.mark.parametrize("active", [False, True])
def test_executor_typed_inspection_preserves_identity_and_lifecycle(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    command_type: str,
    report_type: type[WispEvent],
    id_fields: dict[str, object],
    active: bool,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        running = _RpcRunningCommand("active-1", "prompt", anyio.CancelScope()) if active else None
        parsed = parsed_command({"type": command_type, **id_fields})
        assert parsed.known is not None

        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await executor.dispatch_parsed(parsed, running)
            task_group.cancel_scope.cancel()

        assert result.running_command is running
        assert fixture.coordinator.running_command is running
        assert result.selected_session is None
        assert not result.reset_session
        assert not result.should_shutdown
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            report_type,
            RpcCommandFinished,
        ]
        started, report, finished = fixture.events
        assert isinstance(started, RpcCommandStarted)
        assert isinstance(finished, RpcCommandFinished)
        assert started.command_type == finished.command_type == command_type
        assert started.command_id
        assert report.model_dump()["command_id"] == started.command_id == finished.command_id
        if id_fields.get("id") is not None:
            assert started.command_id == id_fields["id"]
        assert finished.ok is True

    anyio.run(scenario)


def test_executor_reports_commands_from_runtime_registry_without_replacing_running_command(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        fixture.runtime.api.register_command(
            CommandDescriptor(
                name="inspect",
                title="Inspect",
                description="Inspect command registry metadata",
                category=CommandCategory.general,
                aliases=("i", ":inspect"),
                arguments=(
                    CommandArgument(
                        name="target",
                        description="Optional target",
                    ),
                ),
                accepts_arguments=True,
                prefill_on_partial_enter=True,
                order=5,
            )
        )
        running = _RpcRunningCommand("active-1", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await dispatch_parsed(
                executor, {"id": "commands-1", "type": "get_commands"}, running
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is running
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcCommandsReported,
            RpcCommandFinished,
        ]
        report = fixture.events[1]
        assert isinstance(report, RpcCommandsReported)
        assert report.command_id == "commands-1"
        assert report.commands[0].name == "inspect"
        inspected = report.commands[0]
        assert inspected.title == "Inspect"
        assert inspected.description == "Inspect command registry metadata"
        assert inspected.category == "general"
        assert inspected.aliases == ("i", ":inspect")
        assert inspected.slash_command == "/inspect"
        assert inspected.slash_aliases == ("/i", ":inspect")
        assert inspected.arguments[0].name == "target"
        assert inspected.accepts_arguments is True
        assert inspected.prefill_on_partial_enter is True
        assert [descriptor.name for descriptor in report.commands[1:]] == [
            "help",
            "init",
            "queue",
            "compact",
            "context",
            "search",
            "history",
            "update",
            "skills",
            "mcp",
            "permissions",
            "plan",
            "build",
            "model",
            "new",
            "resume",
            "name",
            "clone",
            "tree",
            "unrevert",
            "provider",
            "auth",
            "connect",
            "disconnect",
            "quit",
        ]
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.command_id == "commands-1"
        assert finished.command_type == "get_commands"
        assert finished.ok is True

    anyio.run(scenario)


def test_executor_reports_model_catalog_without_replacing_running_command(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        running = _RpcRunningCommand("active-1", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {"id": "models-1", "type": "get_model_catalog"},
                running,
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is running
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcModelCatalogReported,
            RpcCommandFinished,
        ]
        report = fixture.events[1]
        assert isinstance(report, RpcModelCatalogReported)
        assert report.catalog.selection.provider == "fake"
        assert tuple(provider.name for provider in report.catalog.providers) == tuple(
            provider.name for provider in fixture.runtime.models.providers()
        )
        assert all(
            provider.available == fixture.runtime.providers.is_registered(provider.name)
            for provider in report.catalog.providers
        )

    anyio.run(scenario)


def test_model_catalog_marks_unregistered_providers_without_constructing_deferred_ones() -> None:
    providers = ProviderRegistry()
    current = FakeProvider()
    providers.register(current)
    constructed = False

    def construct_deferred() -> FakeProvider:
        nonlocal constructed
        constructed = True
        return FakeProvider()

    providers.register_factory("deferred", construct_deferred)
    models = ModelRegistry(
        ModelCatalog(
            schema_version=2,
            providers=tuple(
                ModelCatalogProviderEntry(
                    name=name,
                    display_name=name.title(),
                    default_model=model,
                    docs_url=f"https://example.test/{name}",
                    models=(model,),
                )
                for name, model in (
                    ("fake", "fake"),
                    ("catalog-only", "catalog-model"),
                    ("deferred", "deferred-model"),
                )
            ),
        )
    )
    tools = ToolRegistry()
    events = EventBus()
    runtime = WispRuntime(
        providers=providers,
        tools=tools,
        events=events,
        api=ExtensionAPI(
            providers=providers,
            tools=tools,
            events=events,
        ),
        models=models,
    )

    snapshot = rpc_inspection_module.rpc_model_catalog_snapshot(
        runtime=runtime,
        provider=current,
        model="custom-model",
        effort="custom-effort",
    )

    assert [(provider.name, provider.available) for provider in snapshot.providers] == [
        ("fake", True),
        ("catalog-only", False),
        ("deferred", True),
    ]
    assert snapshot.selection.catalog_model is None
    assert snapshot.selection.effort == "custom-effort"
    assert constructed is False


def test_oversized_model_catalog_does_not_block_typed_configuration(tmp_path: Path) -> None:
    provider = FakeProvider()
    providers = ProviderRegistry()
    providers.register(provider)
    model_ids = ("fake", *(f"model-{index}" for index in range(512)))
    models = ModelRegistry(
        ModelCatalog(
            schema_version=2,
            providers=(
                ModelCatalogProviderEntry(
                    name="fake",
                    display_name="Fake",
                    default_model="fake",
                    docs_url="https://example.test/fake",
                    models=model_ids,
                ),
            ),
        )
    )
    tools = ToolRegistry()
    events = EventBus()
    runtime = WispRuntime(
        providers=providers,
        tools=tools,
        events=events,
        api=ExtensionAPI(providers=providers, tools=tools, events=events),
        models=models,
    )
    agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path))
    overrides = _RpcConfigureOverrides()
    discovery_events: list[WispEvent] = []
    rpc_inspection_module.handle_rpc_model_catalog_command(
        GetModelCatalogCommand(id="catalog-1"),
        agent=agent,
        runtime=runtime,
        write_event=discovery_events.append,
    )
    assert not any(isinstance(event, RpcModelCatalogReported) for event in discovery_events)
    assert isinstance(discovery_events[-1], RpcCommandFinished)
    assert discovery_events[-1].ok is False

    emitted: list[WispEvent] = []

    parsed = parsed_command(
        {
            "id": "configure-1",
            "type": "configure",
            "model": "custom-model",
            "effort": "custom-effort",
        }
    )
    command = parsed.known
    assert isinstance(command, ConfigureCommand)
    rpc_configure_module.handle_rpc_configure_command(
        command,
        command_id="configure-1",
        provided_fields=parsed.provided_fields,
        agent=agent,
        runtime=runtime,
        configure_overrides=overrides,
        write_event=emitted.append,
    )

    assert agent.model == "custom-model"
    assert agent.effort == "custom-effort"
    assert overrides.model == "custom-model"
    assert overrides.effort == "custom-effort"
    assert not any(isinstance(event, RpcModelCatalogReported) for event in emitted)
    assert isinstance(emitted[-2], ErrorEvent)
    assert "Configuration applied; model catalog unavailable" in emitted[-2].message
    assert isinstance(emitted[-1], RpcCommandFinished)
    assert emitted[-1].ok is True


def test_executor_reports_active_skill_catalog_without_replacing_running_command(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        fixture.agent.skill_catalog = SkillCatalog(
            entries=(
                SkillEntry(
                    name="review",
                    description="Review [literal] output",
                    source="user:wisp",
                    root=tmp_path / "review",
                ),
            ),
            diagnostics=(
                SkillDiagnostic(
                    code="invalid-yaml",
                    severity="warning",
                    message="broken [literal] metadata",
                    source="project:wisp",
                    path=tmp_path / "bad" / "SKILL.md",
                ),
            ),
        )
        running = _RpcRunningCommand("active-1", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await dispatch_parsed(
                executor, {"id": "skills-1", "type": "get_skills"}, running
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is running
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcSkillsReported,
            RpcCommandFinished,
        ]
        report = fixture.events[1]
        assert isinstance(report, RpcSkillsReported)
        assert report.catalog.entries[0].name == "review"
        assert report.catalog.entries[0].description == "Review [literal] output"
        assert report.catalog.diagnostics[0].message == "broken [literal] metadata"
        assert report.catalog.project_trusted is False

    anyio.run(scenario)


def test_executor_reports_empty_mcp_status_without_replacing_running_command(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        running = _RpcRunningCommand("active-1", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await dispatch_parsed(
                executor, {"id": "mcp-1", "type": "get_mcp_status"}, running
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is running
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcMcpStatusReported,
            RpcCommandFinished,
        ]
        report = fixture.events[1]
        assert isinstance(report, RpcMcpStatusReported)
        assert report.status.servers == ()

    anyio.run(scenario)


def test_executor_commands_reports_registry_failures(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            def fail_commands() -> object:
                raise RuntimeError("registry failed")

            monkeypatch.setattr(fixture.runtime.commands, "all", fail_commands)
            failed = await dispatch_parsed(
                executor, {"id": "commands-1", "type": "get_commands"}, None
            )
            task_group.cancel_scope.cancel()

        assert failed.running_command is None
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_type, event.ok, event.error) for event in finished] == [
            ("get_commands", False, "registry failed"),
        ]

    anyio.run(scenario)


def test_workspace_report_survives_git_failure(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    import wisp.rpc.handlers.inspection as inspection_module

    def broken(cwd: Path) -> str | None:
        raise OSError("git exploded")

    monkeypatch.setattr(inspection_module, "read_current_branch", broken)

    report = anyio.run(inspection_module.rpc_workspace_report, tmp_path)

    assert report.cwd == tmp_path
    assert report.git_branch is None
