# ruff: noqa: F403,F405

"""Tests for the model-registry-backed `/model` auto-switch in RPC configure."""

from __future__ import annotations

import pytest

from tests.cli_support import *


def test_configure_model_auto_switches_provider_when_unambiguous(tmp_path: Path) -> None:
    # "gpt-5.5-pro" only appears in the openai catalog entry (not openai-codex),
    # so it unambiguously resolves and should switch the active provider even
    # though the session started on a different one. Proven by the subsequent
    # prompt failing with the openai provider's specific missing-API-key error
    # (the fake provider never raises this) -- direct evidence the switch
    # actually happened, not just that the configure command reported ok.
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"configure-1","type":"configure","model":"gpt-5.5-pro"}\n'
            '{"id":"cmd-1","type":"prompt","prompt":"hello"}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "OPENAI_API_KEY": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    auto_switched = [r for r in records if r["type"] == "model.provider_auto_switched"]
    assert len(auto_switched) == 1
    assert auto_switched[0]["command_id"] == "configure-1"
    assert auto_switched[0]["provider"] == "openai"
    assert auto_switched[0]["model"] == "gpt-5.5-pro"
    catalog_index = next(
        index
        for index, record in enumerate(records)
        if record["type"] == "rpc.model_catalog" and record["command_id"] == "configure-1"
    )
    configure_finished = next(
        r
        for r in records
        if r["type"] == "rpc.command.finished" and r["command_id"] == "configure-1"
    )
    assert configure_finished["ok"] is True
    assert records[catalog_index]["catalog"]["selection"] == {
        "provider": "openai",
        "model": "gpt-5.5-pro",
        "effective_model": "gpt-5.5-pro",
        "catalog_model": "gpt-5.5-pro",
        "effort": None,
    }
    assert catalog_index < records.index(configure_finished)
    assert any(
        record.get("message")
        == "openai credentials are required; run `/connect` in the TUI or set OPENAI_API_KEY"
        for record in records
    )


def test_configure_model_auto_switches_provider_when_provider_is_explicit_null(
    tmp_path: Path,
) -> None:
    # Regression test: ConfigureCommand.provider is optional (str | None), so a
    # raw JSONL client (not the typed RpcController, which omits None fields via
    # exclude_none=True) can reasonably serialize an omitted provider as
    # "provider": null rather than leaving the key out entirely. The auto-switch
    # gate must treat that the same as omission, not as "an explicit provider
    # was given" -- confirmed the same way as the omitted-key case, via the
    # openai provider's specific missing-API-key error surfacing after the
    # switch.
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"configure-1","type":"configure","provider":null,"model":"gpt-5.5-pro"}\n'
            '{"id":"cmd-1","type":"prompt","prompt":"hello"}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "OPENAI_API_KEY": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    auto_switched = [r for r in records if r["type"] == "model.provider_auto_switched"]
    assert len(auto_switched) == 1
    assert auto_switched[0]["provider"] == "openai"
    assert any(
        record.get("message")
        == "openai credentials are required; run `/connect` in the TUI or set OPENAI_API_KEY"
        for record in records
    )


def test_configure_model_alias_auto_switches_to_its_provider(tmp_path: Path) -> None:
    # Aliases are cataloged for validation and metadata lookup but must preserve
    # their original request value. This proves the registry's alias index also
    # reaches the real configure path rather than only unit-level helpers.
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"configure-1","type":"configure","model":"gemini-flash-latest"}\n'
            '{"id":"cmd-1","type":"prompt","prompt":"hello"}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "GOOGLE_API_KEY": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    auto_switched = [
        record for record in records if record["type"] == "model.provider_auto_switched"
    ]
    assert len(auto_switched) == 1
    assert auto_switched[0]["provider"] == "google"
    assert auto_switched[0]["model"] == "gemini-flash-latest"
    assert any(
        record.get("message")
        == "google credentials are required; run `/connect` in the TUI or set "
        "GOOGLE_API_KEY or GEMINI_API_KEY"
        for record in records
    )


def test_configure_model_leaves_provider_alone_when_model_belongs_to_current_provider(
    tmp_path: Path,
) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"configure-1","type":"configure","model":"gpt-5.5-pro"}\n'
            '{"id":"cmd-1","type":"prompt","prompt":"hello"}\n'
        ),
        env={"WISP_PROVIDER": "openai", "WISP_MODEL": "", "OPENAI_API_KEY": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    # Already on openai -- the (no-op) auto-switch logic must not emit a switch
    # event, error, or otherwise behave any differently than configuring model
    # on an already-matching provider did before this feature existed.
    assert not any(r["type"] == "model.provider_auto_switched" for r in records)
    assert (
        next(
            record
            for record in records
            if record["type"] == "rpc.command.finished" and record["command_id"] == "configure-1"
        )["ok"]
        is True
    )
    assert any(
        record.get("message")
        == "openai credentials are required; run `/connect` in the TUI or set OPENAI_API_KEY"
        for record in records
    )


def test_configure_unknown_model_falls_through_without_error(tmp_path: Path) -> None:
    # Regression guard for the permissive-fallthrough design: a model string
    # absent from every catalog entry must behave exactly as it did before the
    # registry existed -- accepted as free text, no provider switch, no error.
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"configure-1","type":"configure","model":"totally-unknown-model-xyz"}\n'
            '{"id":"cmd-1","type":"prompt","prompt":"hello"}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert not any(r["type"] == "model.provider_auto_switched" for r in records)
    assert (
        next(
            record
            for record in records
            if record["type"] == "rpc.command.finished" and record["command_id"] == "configure-1"
        )["ok"]
        is True
    )
    # The prompt still runs against the unchanged (fake) provider -- proves no
    # provider switch was attempted despite the unresolvable model string.
    assert any(record.get("content") == "fake response to: hello" for record in records)


@pytest.mark.parametrize("provider_fragment", ["", '"provider":null,'])
def test_configure_ambiguous_model_rejects_without_mutating_configuration(
    tmp_path: Path,
    provider_fragment: str,
) -> None:
    # ``gpt-5.6`` is an alias claimed by both openai and openai-codex. The active
    # fake provider cannot disambiguate it, so accepting the model would create
    # a catalog-known invalid pairing. Omitted and explicit-null provider values
    # have the same model-only semantics.
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            f'{{"id":"configure-1","type":"configure",{provider_fragment}'
            '"model":"gpt-5.6","effort":"high",'
            '"auto_compaction_enabled":false,"mode":"plan"}\n'
            '{"id":"state-1","type":"get_state"}\n'
            '{"id":"cmd-1","type":"prompt","prompt":"hello"}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert not any(r["type"] == "model.provider_auto_switched" for r in records)
    message = (
        "Model 'gpt-5.6' is available from multiple providers: openai, openai-codex; "
        "specify provider explicitly"
    )
    error = next(record for record in records if record["type"] == "error")
    assert error["message"] == message
    configure_finished = next(
        record
        for record in records
        if record["type"] == "rpc.command.finished" and record["command_id"] == "configure-1"
    )
    assert configure_finished["ok"] is False
    assert configure_finished["error"] == message
    assert not any(
        record["type"] == "rpc.model_catalog" and record.get("command_id") == "configure-1"
        for record in records
    )
    state = next(record["state"] for record in records if record["type"] == "rpc.state")
    assert state["provider"] == "fake"
    assert state["model"] == "fake"
    assert state["effort"] is None
    assert state["auto_compaction_enabled"] is True
    assert state["mode"] == "build"
    estimate = next(record for record in records if record["type"] == "context.estimated")
    assert estimate["provider"] == "fake"
    assert estimate["model"] == "fake"
    assert any(record.get("content") == "fake response to: hello" for record in records)


def test_configure_ambiguous_model_prefers_current_candidate_provider(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"configure-1","type":"configure","model":"gpt-5.6"}\n'
            '{"id":"state-1","type":"get_state"}\n'
        ),
        env={"WISP_PROVIDER": "openai", "WISP_MODEL": "", "OPENAI_API_KEY": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert not any(record["type"] == "model.provider_auto_switched" for record in records)
    configure_finished = next(
        record
        for record in records
        if record["type"] == "rpc.command.finished" and record["command_id"] == "configure-1"
    )
    assert configure_finished["ok"] is True
    state = next(record["state"] for record in records if record["type"] == "rpc.state")
    assert state["provider"] == "openai"
    assert state["model"] == "gpt-5.6"


def test_configure_explicit_provider_and_model_together_is_unaffected_by_auto_switch(
    tmp_path: Path,
) -> None:
    # ``gpt-5.6`` is ambiguous without a matching current provider, but here an
    # explicit provider is given in the same command. That choice must win,
    # preserving support for custom providers that accept cataloged model ids.
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"configure-1","type":"configure","provider":"fake","model":"gpt-5.6"}\n'
            '{"id":"cmd-1","type":"prompt","prompt":"hello"}\n'
        ),
        env={"WISP_PROVIDER": "openai", "WISP_MODEL": "", "OPENAI_API_KEY": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert (
        next(
            record
            for record in records
            if record["type"] == "rpc.command.finished" and record["command_id"] == "configure-1"
        )["ok"]
        is True
    )
    assert any(record.get("content") == "fake response to: hello" for record in records)
