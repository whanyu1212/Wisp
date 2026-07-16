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
