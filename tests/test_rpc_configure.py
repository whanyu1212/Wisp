# ruff: noqa: F403,F405

"""Tests for the model-registry-backed `/model` auto-switch in RPC configure."""

from __future__ import annotations

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
    configure_finished = next(
        r
        for r in records
        if r["type"] == "rpc.command.finished" and r["command_id"] == "configure-1"
    )
    assert configure_finished["ok"] is True
    assert any(
        record.get("message") == "OPENAI_API_KEY is required when using the openai provider"
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
        record.get("message") == "OPENAI_API_KEY is required when using the openai provider"
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
        == "GOOGLE_API_KEY or GEMINI_API_KEY is required when using the google provider"
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
    assert records[1]["ok"] is True
    assert any(
        record.get("message") == "OPENAI_API_KEY is required when using the openai provider"
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
    assert records[1]["ok"] is True
    # The prompt still runs against the unchanged (fake) provider -- proves no
    # provider switch was attempted despite the unresolvable model string.
    assert any(record.get("content") == "fake response to: hello" for record in records)


def test_configure_ambiguous_model_falls_through_without_error(tmp_path: Path) -> None:
    # ``gpt-5.6`` is an alias claimed by both openai and openai-codex, and
    # "fake" (the active provider) doesn't match either -- this must not raise
    # or silently guess a winner.
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"configure-1","type":"configure","model":"gpt-5.6"}\n'
            '{"id":"cmd-1","type":"prompt","prompt":"hello"}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert not any(r["type"] == "model.provider_auto_switched" for r in records)
    assert records[1]["ok"] is True
    assert any(record.get("content") == "fake response to: hello" for record in records)


def test_configure_explicit_provider_and_model_together_is_unaffected_by_auto_switch(
    tmp_path: Path,
) -> None:
    # "gpt-5.5-pro" would auto-switch to openai if provider were omitted (see
    # test_configure_model_auto_switches_provider_when_unambiguous above), but
    # here provider is given explicitly as "fake" in the same command -- that
    # explicit choice must win, proven by the prompt succeeding with the fake
    # provider's response instead of failing on a missing OpenAI key.
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"configure-1","type":"configure","provider":"fake","model":"gpt-5.5-pro"}\n'
            '{"id":"cmd-1","type":"prompt","prompt":"hello"}\n'
        ),
        env={"WISP_PROVIDER": "openai", "WISP_MODEL": "", "OPENAI_API_KEY": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert records[1]["ok"] is True
    assert any(record.get("content") == "fake response to: hello" for record in records)
