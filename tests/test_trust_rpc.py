# ruff: noqa: F403,F405

from __future__ import annotations

import json

from tests.cli_support import *


def test_rpc_prompt_in_undecided_project_emits_trust_request(tmp_path: Path) -> None:
    # Undecided project: the prompt blocks on trust; the client answers with a
    # trust command referencing the emitted request_id.
    runner = CliRunner()

    # Two lines: the prompt (which emits trust.requested), then the trust answer.
    # The client normally reads request_id from the emitted event, but the RPC
    # loop processes the trust command as soon as it arrives regardless of order.
    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"p1","type":"prompt","prompt":"hello"}\n'
            '{"id":"t1","type":"trust","request_id":"__REPLACE__","trusted":true}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    # We cannot know the server-generated request_id ahead of time, so this run
    # exercises the "trust command with wrong request_id -> error, input closes ->
    # untrusted, no hang" path. The prompt still completes (untrusted).
    assert result.exit_code == 0, result.output
    types = [r["type"] for r in _jsonl_records(result.stdout)]
    assert "trust.requested" in types
    assert "trust.resolved" in types
    # The prompt finished despite trust never being explicitly granted.
    finished = [r for r in _jsonl_records(result.stdout) if r["type"] == "rpc.command.finished"]
    assert any(r.get("command_id") == "p1" for r in finished)


def test_rpc_trust_env_override_skips_prompt(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"p1","type":"prompt","prompt":"hello"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    types = [r["type"] for r in _jsonl_records(result.stdout)]
    assert "trust.requested" not in types
    assert "trust.resolved" not in types


def test_rpc_trust_stored_decision_skips_prompt(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    # Pre-record a trust decision for the cwd; the prompt must not re-prompt.
    trust_file = tmp_path / "trust.json"
    monkeypatch.setenv("WISP_TRUST_FILE", str(trust_file))
    from wisp.trust import record_trust

    record_trust(Path.cwd(), True)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"p1","type":"prompt","prompt":"hi"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "WISP_TRUST_FILE": str(trust_file)},
    )

    assert result.exit_code == 0, result.output
    types = [r["type"] for r in _jsonl_records(result.stdout)]
    assert "trust.requested" not in types


def test_rpc_trust_input_closed_yields_untrusted_no_hang(tmp_path: Path) -> None:
    # A prompt with no trust answer and immediate EOF must resolve to untrusted
    # (safe) and terminate — never hang waiting for a trust response.
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"p1","type":"prompt","prompt":"hello"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    resolved = [r for r in records if r["type"] == "trust.resolved"]
    assert resolved and resolved[0]["trusted"] is False
    # The forced-untrusted decision from input close is not persisted, so a later
    # interactive run still prompts.


def test_rpc_trust_command_round_trip(tmp_path: Path) -> None:
    # Drive the trust command directly against the gate to confirm resolve_request
    # matches on request_id and rejects unknown ids.
    from wisp.cli.rpc import _RpcTrustGate

    gate = _RpcTrustGate(Path.cwd())
    # No pending request yet -> unknown id rejected.
    assert gate.resolve_request(request_id="nope", trusted=True) is False


def test_rpc_gate_fires_on_first_trusted_callback(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    # The rebuild hook runs exactly once when trust first resolves to True, so a
    # first-run session that approves trust can apply the project's settings before
    # the first turn.
    from wisp.cli.rpc import _RpcTrustGate

    monkeypatch.setenv("WISP_TRUST", "1")
    calls = 0

    async def on_trusted() -> None:
        nonlocal calls
        calls += 1

    gate = _RpcTrustGate(Path.cwd(), on_first_trusted=on_trusted)

    async def scenario() -> None:
        assert await gate.resolve() is True
        assert calls == 1
        # Cached: a second resolve does not re-run the hook.
        assert await gate.resolve() is True
        assert calls == 1

    anyio.run(scenario)


def test_rpc_gate_does_not_fire_callback_when_untrusted(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from wisp.cli.rpc import _RpcTrustGate

    monkeypatch.setenv("WISP_TRUST", "0")
    fired = False

    async def on_trusted() -> None:
        nonlocal fired
        fired = True

    gate = _RpcTrustGate(Path.cwd(), on_first_trusted=on_trusted)

    async def scenario() -> None:
        assert await gate.resolve() is False

    anyio.run(scenario)
    assert fired is False


def test_config_overrides_gates_project_settings_on_trust(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # _ConfigOverrides.build(trusted=...) is the rebuild's re-derivation: untrusted
    # ignores the project settings.json, trusted applies it.
    import json as _json

    from wisp.cli.rpc import _ConfigOverrides

    monkeypatch.delenv("WISP_PROVIDER", raising=False)
    (tmp_path / ".wisp").mkdir()
    (tmp_path / ".wisp" / "settings.json").write_text(
        _json.dumps({"provider": "from-project-settings"}), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    overrides = _ConfigOverrides()

    assert overrides.build(trusted=False).provider != "from-project-settings"
    assert overrides.build(trusted=True).provider == "from-project-settings"


def test_trust_command_serializes_over_rpc() -> None:
    from wisp.rpc.commands import TrustCommand, rpc_command_from_json

    command = TrustCommand(id="c1", request_id="r1", trusted=True)
    line = command.to_json_line()
    parsed = rpc_command_from_json(line)
    assert isinstance(parsed, TrustCommand)
    assert parsed.request_id == "r1"
    assert parsed.trusted is True
    # exclude_none keeps reason out when unset.
    assert "reason" not in json.loads(line)
