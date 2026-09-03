# ruff: noqa: F403,F405

"""Tests for the RPC `configure` command's `effort` field.

`FakeProvider` (used by the full-CLI `CliRunner` pattern in
`test_rpc_configure.py`) has no observable trace of `effort` in its output,
so it cannot prove the value actually reached the provider. `CapturingProvider`
can -- these tests exercise `_configure_rpc` directly against a
runtime built around it, mirroring how `test_coding_session.py` already proves
`CodingSession.effort` reaches the provider; what's missing is specifically
whether the RPC command layer correctly sets `agent.effort`.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from tests.cli_support import *
from tests.cli_support import _test_model_registry
from tests.test_coding_session import CapturingProvider
from wisp.events import ErrorEvent, RpcCommandFinished, WispEvent
from wisp.rpc.commands import ConfigureCommand, RpcCommandAdapter
from wisp.rpc.configuration import _RpcConfigureOverrides
from wisp.rpc.execution import handle_rpc_configure_command


def _configure_rpc(
    payload: dict[str, object],
    *,
    command_id: str,
    agent: CodingSession,
    runtime: WispRuntime,
    configure_overrides: _RpcConfigureOverrides | None = None,
    write_event: Callable[[WispEvent], None] = lambda _event: None,
) -> None:
    submitted = {"id": command_id, "type": "configure", **payload}
    command = RpcCommandAdapter.validate_json(json.dumps(submitted))
    assert isinstance(command, ConfigureCommand)
    handle_rpc_configure_command(
        command,
        command_id=command_id,
        provided_fields=frozenset(submitted),
        agent=agent,
        runtime=runtime,
        configure_overrides=configure_overrides,
        write_event=write_event,
    )


def _runtime_with_capturing_provider(provider: CapturingProvider) -> WispRuntime:
    providers = ProviderRegistry()
    providers.register(provider)
    return WispRuntime(
        providers=providers,
        tools=ToolRegistry(),
        events=EventBus(),
        api=ExtensionAPI(providers=providers, tools=ToolRegistry(), events=EventBus()),
        models=_test_model_registry(),
    )


class _SecondCapturingProvider(CapturingProvider):
    name = "capturing-2"


def test_configure_effort_sets_agent_effort(tmp_path: Path) -> None:
    provider = CapturingProvider()
    runtime = _runtime_with_capturing_provider(provider)
    agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path))

    _configure_rpc(
        {"provider": "capturing", "effort": "high"},
        command_id="configure-1",
        agent=agent,
        runtime=runtime,
    )

    assert agent.effort == "high"


def test_configure_auto_compaction_sets_agent_policy(tmp_path: Path) -> None:
    provider = CapturingProvider()
    runtime = _runtime_with_capturing_provider(provider)
    agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path))

    _configure_rpc(
        {"auto_compaction_enabled": False},
        command_id="configure-1",
        agent=agent,
        runtime=runtime,
    )

    assert agent.auto_compaction_enabled is False
    assert agent.state_snapshot().auto_compaction_enabled is False

    _configure_rpc(
        {"auto_compaction_enabled": True},
        command_id="configure-2",
        agent=agent,
        runtime=runtime,
    )

    assert agent.auto_compaction_enabled is True


def test_configure_explicit_null_effort_resets_with_another_mutation(tmp_path: Path) -> None:
    provider = CapturingProvider()
    runtime = _runtime_with_capturing_provider(provider)
    agent = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path),
        effort="high",
    )

    _configure_rpc(
        {"effort": None, "auto_compaction_enabled": False},
        command_id="configure-1",
        agent=agent,
        runtime=runtime,
    )

    assert agent.effort is None
    assert agent.auto_compaction_enabled is False


def test_configure_effort_alone_is_accepted_without_model_or_provider(tmp_path: Path) -> None:
    # Regression guard: effort-only configure must not hit the "requires
    # provider or model" rejection path that predates this field.
    provider = CapturingProvider()
    runtime = _runtime_with_capturing_provider(provider)
    agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path))

    _configure_rpc(
        {"effort": "medium"},
        command_id="configure-1",
        agent=agent,
        runtime=runtime,
    )

    assert agent.effort == "medium"


def test_configure_model_change_without_effort_resets_agent_effort(tmp_path: Path) -> None:
    # Regression test: effort support is per-model, not just per-provider
    # (catalog.toml deliberately omits some models, e.g. claude-haiku-4-5,
    # from effort_levels entirely) -- a model change that stays on the same
    # provider must still reset a previously-set effort tier, or it could
    # reach a model that doesn't support it on the next prompt.
    provider = CapturingProvider()
    runtime = _runtime_with_capturing_provider(provider)
    agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path), effort="low")

    _configure_rpc(
        {"model": "some-model"},
        command_id="configure-1",
        agent=agent,
        runtime=runtime,
    )

    assert agent.effort is None


def test_configure_effort_only_command_leaves_it_alone_when_touched(tmp_path: Path) -> None:
    # A configure command that touches neither `provider` nor `model` must
    # not accidentally reset effort as a side effect of some other field.
    provider = CapturingProvider()
    runtime = _runtime_with_capturing_provider(provider)
    agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path), effort="low")

    _configure_rpc(
        {"effort": "high"},
        command_id="configure-1",
        agent=agent,
        runtime=runtime,
    )

    assert agent.effort == "high"


def test_configure_model_change_with_explicit_effort_keeps_the_new_value(tmp_path: Path) -> None:
    provider = CapturingProvider()
    runtime = _runtime_with_capturing_provider(provider)
    agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path), effort="low")

    _configure_rpc(
        {"model": "some-model", "effort": "high"},
        command_id="configure-1",
        agent=agent,
        runtime=runtime,
    )

    assert agent.effort == "high"


def test_configure_overrides_records_effort(tmp_path: Path) -> None:

    provider = CapturingProvider()
    runtime = _runtime_with_capturing_provider(provider)
    agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path))
    overrides = _RpcConfigureOverrides()

    _configure_rpc(
        {"effort": "xhigh"},
        command_id="configure-1",
        agent=agent,
        runtime=runtime,
        configure_overrides=overrides,
    )

    assert overrides.effort == "xhigh"
    assert overrides.has_effort is True
    assert overrides.effective_effort("default") == "xhigh"


def test_configure_overrides_effective_effort_falls_back_to_default_when_unset() -> None:

    overrides = _RpcConfigureOverrides()

    assert overrides.effective_effort("fallback") == "fallback"


def test_model_only_configure_does_not_override_provider_without_auto_switch(
    tmp_path: Path,
) -> None:

    provider = CapturingProvider()
    runtime = _runtime_with_capturing_provider(provider)
    agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path))
    overrides = _RpcConfigureOverrides()

    _configure_rpc(
        {"model": "custom-model"},
        command_id="configure-1",
        agent=agent,
        runtime=runtime,
        configure_overrides=overrides,
    )

    assert overrides.provider is None
    assert overrides.model == "custom-model"


def test_ambiguous_model_configure_is_atomic_across_agent_and_overrides(tmp_path: Path) -> None:
    provider = CapturingProvider()
    runtime = _runtime_with_capturing_provider(provider)
    agent = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path),
        model="capturing-model",
        effort="low",
        auto_compaction_enabled=False,
    )
    overrides = _RpcConfigureOverrides()
    events: list[WispEvent] = []

    _configure_rpc(
        {
            "model": "gpt-5.6",
            "effort": "high",
            "auto_compaction_enabled": True,
            "mode": "plan",
        },
        command_id="configure-1",
        agent=agent,
        runtime=runtime,
        configure_overrides=overrides,
        write_event=events.append,
    )

    assert agent.provider is provider
    assert agent.model == "capturing-model"
    assert agent.effort == "low"
    assert agent.auto_compaction_enabled is False
    assert agent.mode == "build"
    assert overrides == _RpcConfigureOverrides()
    assert isinstance(events[0], ErrorEvent)
    assert events[0].message == (
        "Model 'gpt-5.6' is available from multiple providers: openai, openai-codex; "
        "specify provider explicitly"
    )
    assert isinstance(events[1], RpcCommandFinished)
    assert events[1].ok is False


def test_model_only_configure_rejects_catalog_provider_missing_from_runtime(
    tmp_path: Path,
) -> None:
    provider = CapturingProvider()
    runtime = _runtime_with_capturing_provider(provider)
    agent = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path),
        model="capturing-model",
        effort="high",
        auto_compaction_enabled=False,
    )
    overrides = _RpcConfigureOverrides()
    events: list[WispEvent] = []

    _configure_rpc(
        {"model": "gpt-5.5-pro"},
        command_id="configure-1",
        agent=agent,
        runtime=runtime,
        configure_overrides=overrides,
        write_event=events.append,
    )

    assert agent.provider is provider
    assert agent.model == "capturing-model"
    assert agent.effort == "high"
    assert agent.auto_compaction_enabled is False
    assert overrides == _RpcConfigureOverrides()
    assert isinstance(events[0], ErrorEvent)
    assert events[0].message == (
        "Model 'gpt-5.5-pro' resolves to provider 'openai', which is not available"
    )
    assert isinstance(events[1], RpcCommandFinished)
    assert events[1].ok is False


def test_configure_clear_effort_resets_agent_effort_to_none(tmp_path: Path) -> None:
    # Regression test: effort=None is indistinguishable on the wire from
    # never having set effort at all, so clear_effort is the only way a
    # client can explicitly reset a previously-configured effort tier back
    # to the provider's own default.
    provider = CapturingProvider()
    runtime = _runtime_with_capturing_provider(provider)
    agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path), effort="high")

    _configure_rpc(
        {"clear_effort": True},
        command_id="configure-1",
        agent=agent,
        runtime=runtime,
    )

    assert agent.effort is None


def test_configure_clear_effort_alone_is_accepted_without_model_or_provider(
    tmp_path: Path,
) -> None:
    provider = CapturingProvider()
    runtime = _runtime_with_capturing_provider(provider)
    agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path), effort="high")

    _configure_rpc(
        {"clear_effort": True},
        command_id="configure-1",
        agent=agent,
        runtime=runtime,
    )

    assert agent.effort is None


def test_configure_overrides_records_clear_effort(tmp_path: Path) -> None:

    provider = CapturingProvider()
    runtime = _runtime_with_capturing_provider(provider)
    agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path), effort="high")
    overrides = _RpcConfigureOverrides()

    _configure_rpc(
        {"clear_effort": True},
        command_id="configure-1",
        agent=agent,
        runtime=runtime,
        configure_overrides=overrides,
    )

    assert overrides.effort is None
    assert overrides.has_effort is True
    assert overrides.effective_effort("default") is None


def test_configure_switching_provider_resets_stale_effort(tmp_path: Path) -> None:
    # Regression test: effort tiers are provider-native, non-normalized
    # strings (e.g. Google's "MEDIUM" vs OpenAI's lowercase "medium") -- a
    # tier chosen for the old provider must not survive a provider switch
    # that doesn't also specify a new effort, or it reaches the new
    # provider's API unvalidated on the next prompt. Mirrors the existing
    # reset already applied to `model` in the same situation.
    first_provider = CapturingProvider()
    second_provider = _SecondCapturingProvider()
    providers = ProviderRegistry()
    providers.register(first_provider)
    providers.register(second_provider)
    runtime = WispRuntime(
        providers=providers,
        tools=ToolRegistry(),
        events=EventBus(),
        api=ExtensionAPI(providers=providers, tools=ToolRegistry(), events=EventBus()),
        models=_test_model_registry(),
    )
    agent = CodingSession(
        provider=first_provider, sessions=JsonlSessionStore(tmp_path), effort="MEDIUM"
    )

    _configure_rpc(
        {"provider": "capturing-2"},
        command_id="configure-1",
        agent=agent,
        runtime=runtime,
    )

    assert agent.provider is second_provider
    assert agent.effort is None


def test_configure_switching_provider_with_explicit_effort_keeps_the_new_value(
    tmp_path: Path,
) -> None:
    first_provider = CapturingProvider()
    second_provider = _SecondCapturingProvider()
    providers = ProviderRegistry()
    providers.register(first_provider)
    providers.register(second_provider)
    runtime = WispRuntime(
        providers=providers,
        tools=ToolRegistry(),
        events=EventBus(),
        api=ExtensionAPI(providers=providers, tools=ToolRegistry(), events=EventBus()),
        models=_test_model_registry(),
    )
    agent = CodingSession(
        provider=first_provider, sessions=JsonlSessionStore(tmp_path), effort="MEDIUM"
    )

    _configure_rpc(
        {"provider": "capturing-2", "effort": "medium"},
        command_id="configure-1",
        agent=agent,
        runtime=runtime,
    )

    assert agent.provider is second_provider
    assert agent.effort == "medium"


class _FakeNamedProvider(CapturingProvider):
    name = "fake"


class _OpenAINamedProvider(CapturingProvider):
    name = "openai"


def test_configure_model_only_auto_switch_resets_stale_effort(tmp_path: Path) -> None:

    # Regression test: _auto_switch_provider_for_model changes agent.provider
    # via a separate code path from the explicit-provider branch above --
    # a configure command carrying only `model` (no `provider` field) can
    # still move the session to a different provider when the model
    # unambiguously belongs elsewhere. That path must reset stale effort the
    # same way, or a Google-native "MEDIUM" (say) survives an auto-switch to
    # OpenAI and reaches its API unvalidated on the next prompt.
    fake_provider = _FakeNamedProvider()
    openai_provider = _OpenAINamedProvider()
    providers = ProviderRegistry()
    providers.register(fake_provider)
    providers.register(openai_provider)
    runtime = WispRuntime(
        providers=providers,
        tools=ToolRegistry(),
        events=EventBus(),
        api=ExtensionAPI(providers=providers, tools=ToolRegistry(), events=EventBus()),
        models=_test_model_registry(),
    )
    agent = CodingSession(
        provider=fake_provider, sessions=JsonlSessionStore(tmp_path), effort="MEDIUM"
    )
    overrides = _RpcConfigureOverrides()

    # "gpt-5.5-pro" unambiguously belongs to openai in the built-in catalog
    # (confirmed in tests/test_rpc_configure.py's auto-switch tests) -- no
    # explicit "provider" field here, only "model".
    _configure_rpc(
        {"model": "gpt-5.5-pro"},
        command_id="configure-1",
        agent=agent,
        runtime=runtime,
        configure_overrides=overrides,
    )

    assert agent.provider is openai_provider
    assert agent.effort is None
    assert overrides.provider == "openai"


def test_configure_model_only_auto_switch_filters_unsupported_explicit_effort(
    tmp_path: Path,
) -> None:
    fake_provider = _FakeNamedProvider()
    openai_provider = _OpenAINamedProvider()
    providers = ProviderRegistry()
    providers.register(fake_provider)
    providers.register(openai_provider)
    runtime = WispRuntime(
        providers=providers,
        tools=ToolRegistry(),
        events=EventBus(),
        api=ExtensionAPI(providers=providers, tools=ToolRegistry(), events=EventBus()),
        models=_test_model_registry(),
    )
    agent = CodingSession(
        provider=fake_provider, sessions=JsonlSessionStore(tmp_path), effort="MEDIUM"
    )

    _configure_rpc(
        {"model": "gpt-5.5-pro", "effort": "medium"},
        command_id="configure-1",
        agent=agent,
        runtime=runtime,
    )

    assert agent.provider is openai_provider
    assert agent.effort is None
