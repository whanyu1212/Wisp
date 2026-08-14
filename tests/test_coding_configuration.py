from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import anyio
import pytest

from wisp.coding import (
    CodingSession,
    CodingSessionConfiguration,
    resolve_coding_session_configuration,
)
from wisp.config import WispConfig
from wisp.providers.catalog import ModelRegistry, effective_catalog
from wisp.providers.events import ProviderResponseCompleted, ProviderResponseStarted, ProviderUsage
from wisp.providers.fake import FakeProvider, ScriptedProvider
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ProviderRegistry, ToolRegistry
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy


class _OpenAIProvider(FakeProvider):
    name = "openai"


def _models() -> ModelRegistry:
    return ModelRegistry(effective_catalog(home_dir=Path("/nonexistent-test-home")))


def test_resolver_validates_default_effort_but_preserves_explicit_override(tmp_path: Path) -> None:
    providers = ProviderRegistry()
    providers.register(_OpenAIProvider())
    config = WispConfig(
        provider="openai",
        model="gpt-5.5",
        effort="HIGH",  # Google-style tier, invalid for this cataloged OpenAI model.
        auth_path=tmp_path / "auth.json",
    )

    default_configuration = resolve_coding_session_configuration(
        config,
        providers=providers,
        models=_models(),
        trusted=False,
    )
    explicit_configuration = resolve_coding_session_configuration(
        config,
        providers=providers,
        models=_models(),
        trusted=True,
        effort="custom-tier",
        has_effort=True,
    )

    assert default_configuration.effort is None
    assert explicit_configuration.effort == "custom-tier"
    assert explicit_configuration.trusted is True
    assert (
        config.auth_path.resolve().as_posix() in explicit_configuration.tool_context.protected_paths
    )


def test_reconfigure_updates_dynamic_settings_and_preserves_live_resources(tmp_path: Path) -> None:
    initial_provider = FakeProvider()
    replacement_provider = _OpenAIProvider()
    events = EventBus()
    sessions = JsonlSessionStore(tmp_path / "sessions")
    tools = ToolRegistry()
    policy = ToolPolicy.allow_no_tools()
    approval_policy = ToolApprovalPolicy.require_approval()
    initial_context = ToolContext(cwd=tmp_path, protected_paths=(".env",))
    replacement_context = ToolContext(cwd=tmp_path, protected_paths=("secret.txt",))
    agent = CodingSession(
        provider=initial_provider,
        sessions=sessions,
        events=events,
        model="initial-model",
        effort="initial-effort",
        tool_registry=tools,
        tool_policy=policy,
        tool_approval_policy=approval_policy,
        tool_context=initial_context,
        trusted=False,
        context_reserve_tokens=10,
        auto_compaction_enabled=False,
    )

    agent.reconfigure(
        CodingSessionConfiguration(
            provider=replacement_provider,
            model="replacement-model",
            effort="replacement-effort",
            models=None,
            tool_context=replacement_context,
            trusted=True,
            context_reserve_tokens=20,
            auto_compaction_enabled=True,
        )
    )

    assert agent.provider is replacement_provider
    assert agent.model == "replacement-model"
    assert agent.effort == "replacement-effort"
    assert agent.tool_context is replacement_context
    assert agent.trusted is True
    assert agent.context_reserve_tokens == 20
    assert agent.auto_compaction_enabled is True
    assert agent.sessions is sessions
    assert agent.events is events
    assert agent.tool_registry is tools
    assert agent.tool_policy is policy
    assert agent.tool_approval_policy is approval_policy


def test_from_configuration_preserves_resolved_tool_context(tmp_path: Path) -> None:
    provider = FakeProvider()
    tool_context = ToolContext(cwd=tmp_path, protected_paths=("secret.txt",))
    configuration = CodingSessionConfiguration(
        provider=provider,
        model=None,
        effort=None,
        models=None,
        tool_context=tool_context,
        trusted=True,
        context_reserve_tokens=16_384,
        auto_compaction_enabled=True,
    )

    agent = CodingSession.from_configuration(
        configuration,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
    )

    assert agent.tool_context is tool_context


def test_reconfigure_rejects_changes_while_an_operation_is_active(tmp_path: Path) -> None:
    provider = FakeProvider()
    agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path))
    replacement = CodingSessionConfiguration(
        provider=provider,
        model=None,
        effort=None,
        models=None,
        tool_context=ToolContext(cwd=tmp_path),
        trusted=False,
        context_reserve_tokens=16_384,
        auto_compaction_enabled=True,
    )

    async def scenario() -> None:
        events = agent.run("hello")
        assert (await anext(events)).type == "agent.started"
        with pytest.raises(RuntimeError, match="CodingSession is busy"):
            agent.reconfigure(replacement)
        await events.aclose()
        agent.reconfigure(replacement)

    anyio.run(scenario)


def test_effort_reconfigure_preserves_valid_context_observation(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="scripted"),
                ProviderResponseCompleted(
                    content="done",
                    usage=ProviderUsage(input_tokens=70, output_tokens=11, total_tokens=81),
                ),
            ]
        ]
    )
    sessions = JsonlSessionStore(tmp_path)
    session = sessions.create()
    agent = CodingSession(provider=provider, sessions=sessions)

    async def scenario() -> None:
        _events = [event async for event in agent.run("hello", session=session)]

    anyio.run(scenario)
    observation = agent._context_observations[session.session_id]  # noqa: SLF001

    agent.reconfigure(replace(agent.configuration, effort="high"))

    assert agent._context_observations[session.session_id] is observation  # noqa: SLF001


@pytest.mark.parametrize("invalid_limit", [-1, True])
def test_coding_session_rejects_invalid_tool_iteration_limit(
    tmp_path: Path, invalid_limit: object
) -> None:
    with pytest.raises(ValueError, match="max_tool_iterations"):
        CodingSession(
            provider=FakeProvider(),
            sessions=JsonlSessionStore(tmp_path),
            max_tool_iterations=invalid_limit,  # type: ignore[arg-type]
        )


def test_coding_session_accepts_zero_tool_iteration_limit(tmp_path: Path) -> None:
    agent = CodingSession(
        provider=FakeProvider(),
        sessions=JsonlSessionStore(tmp_path),
        max_tool_iterations=0,
    )
    assert agent.max_tool_iterations == 0


@pytest.mark.parametrize("invalid_reserve", [-1, True])
def test_reconfigure_rejects_invalid_reserve_without_partial_mutation(
    tmp_path: Path, invalid_reserve: object
) -> None:
    initial_provider = FakeProvider()
    replacement_provider = _OpenAIProvider()
    agent = CodingSession(
        provider=initial_provider,
        sessions=JsonlSessionStore(tmp_path),
        model="initial-model",
        tool_context=ToolContext(cwd=tmp_path, protected_paths=("initial",)),
        trusted=False,
        context_reserve_tokens=10,
    )
    before = agent.configuration
    invalid = CodingSessionConfiguration(
        provider=replacement_provider,
        model="replacement-model",
        effort="replacement-effort",
        models=None,
        tool_context=ToolContext(cwd=tmp_path, protected_paths=("replacement",)),
        trusted=True,
        context_reserve_tokens=invalid_reserve,  # type: ignore[arg-type]
        auto_compaction_enabled=False,
    )
    with pytest.raises(ValueError, match="context_reserve_tokens"):
        agent.reconfigure(invalid)
    assert agent.configuration == before
