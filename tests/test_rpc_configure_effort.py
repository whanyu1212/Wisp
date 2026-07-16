# ruff: noqa: F403,F405

"""Tests for the RPC `configure` command's `effort` field.

`FakeProvider` (used by the full-CLI `CliRunner` pattern in
`test_rpc_configure.py`) has no observable trace of `effort` in its output,
so it cannot prove the value actually reached the provider. `CapturingProvider`
can -- these tests exercise `_handle_rpc_configure_command` directly against a
runtime built around it, mirroring how `test_coding_session.py` already proves
`CodingSession.effort` reaches the provider; what's missing is specifically
whether the RPC command layer correctly sets `agent.effort`.
"""

from __future__ import annotations

from tests.cli_support import *
from tests.cli_support import _test_model_registry
from tests.test_coding_session import CapturingProvider
from wisp.cli.rpc import _handle_rpc_configure_command


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

    _handle_rpc_configure_command(
        {"provider": "capturing", "effort": "high"},
        command_id="configure-1",
        command_type="configure",
        agent=agent,
        runtime=runtime,
    )

    assert agent.effort == "high"


def test_configure_effort_alone_is_accepted_without_model_or_provider(tmp_path: Path) -> None:
    # Regression guard: effort-only configure must not hit the "requires
    # provider or model" rejection path that predates this field.
    provider = CapturingProvider()
    runtime = _runtime_with_capturing_provider(provider)
    agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path))

    _handle_rpc_configure_command(
        {"effort": "medium"},
        command_id="configure-1",
        command_type="configure",
        agent=agent,
        runtime=runtime,
    )

    assert agent.effort == "medium"


def test_configure_without_effort_key_leaves_agent_effort_unchanged(tmp_path: Path) -> None:
    provider = CapturingProvider()
    runtime = _runtime_with_capturing_provider(provider)
    agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path), effort="low")

    _handle_rpc_configure_command(
        {"model": "some-model"},
        command_id="configure-1",
        command_type="configure",
        agent=agent,
        runtime=runtime,
    )

    assert agent.effort == "low"


def test_configure_rejects_non_string_effort(tmp_path: Path) -> None:
    provider = CapturingProvider()
    runtime = _runtime_with_capturing_provider(provider)
    agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path))

    # _write_rpc_command_error writes a JSON event to stdout as a side effect;
    # the observable contract under test here is that agent.effort is left
    # untouched when validation rejects the command, not the emitted event
    # shape (already covered for provider/model by existing tests).
    _handle_rpc_configure_command(
        {"effort": 5},
        command_id="configure-1",
        command_type="configure",
        agent=agent,
        runtime=runtime,
    )

    assert agent.effort is None


def test_configure_overrides_records_effort(tmp_path: Path) -> None:
    from wisp.cli.rpc import _RpcConfigureOverrides

    provider = CapturingProvider()
    runtime = _runtime_with_capturing_provider(provider)
    agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path))
    overrides = _RpcConfigureOverrides()

    _handle_rpc_configure_command(
        {"effort": "xhigh"},
        command_id="configure-1",
        command_type="configure",
        agent=agent,
        runtime=runtime,
        configure_overrides=overrides,
    )

    assert overrides.effort == "xhigh"
    assert overrides.has_effort is True
    assert overrides.effective_effort("default") == "xhigh"


def test_configure_overrides_effective_effort_falls_back_to_default_when_unset() -> None:
    from wisp.cli.rpc import _RpcConfigureOverrides

    overrides = _RpcConfigureOverrides()

    assert overrides.effective_effort("fallback") == "fallback"


def test_configure_clear_effort_resets_agent_effort_to_none(tmp_path: Path) -> None:
    # Regression test: effort=None is indistinguishable on the wire from
    # never having set effort at all, so clear_effort is the only way a
    # client can explicitly reset a previously-configured effort tier back
    # to the provider's own default.
    provider = CapturingProvider()
    runtime = _runtime_with_capturing_provider(provider)
    agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path), effort="high")

    _handle_rpc_configure_command(
        {"clear_effort": True},
        command_id="configure-1",
        command_type="configure",
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

    _handle_rpc_configure_command(
        {"clear_effort": True},
        command_id="configure-1",
        command_type="configure",
        agent=agent,
        runtime=runtime,
    )

    assert agent.effort is None


def test_configure_overrides_records_clear_effort(tmp_path: Path) -> None:
    from wisp.cli.rpc import _RpcConfigureOverrides

    provider = CapturingProvider()
    runtime = _runtime_with_capturing_provider(provider)
    agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path), effort="high")
    overrides = _RpcConfigureOverrides()

    _handle_rpc_configure_command(
        {"clear_effort": True},
        command_id="configure-1",
        command_type="configure",
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

    _handle_rpc_configure_command(
        {"provider": "capturing-2"},
        command_id="configure-1",
        command_type="configure",
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

    _handle_rpc_configure_command(
        {"provider": "capturing-2", "effort": "medium"},
        command_id="configure-1",
        command_type="configure",
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

    # "gpt-5.5-pro" unambiguously belongs to openai in the built-in catalog
    # (confirmed in tests/test_rpc_configure.py's auto-switch tests) -- no
    # explicit "provider" field here, only "model".
    _handle_rpc_configure_command(
        {"model": "gpt-5.5-pro"},
        command_id="configure-1",
        command_type="configure",
        agent=agent,
        runtime=runtime,
    )

    assert agent.provider is openai_provider
    assert agent.effort is None


def test_configure_model_only_auto_switch_with_explicit_effort_keeps_the_new_value(
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

    _handle_rpc_configure_command(
        {"model": "gpt-5.5-pro", "effort": "medium"},
        command_id="configure-1",
        command_type="configure",
        agent=agent,
        runtime=runtime,
    )

    assert agent.provider is openai_provider
    assert agent.effort == "medium"
