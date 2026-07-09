# ruff: noqa: F403,F405

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from typing import Any, cast

import pytest

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


@pytest.mark.parametrize(
    ("configure_command", "expected_provider", "expected_model"),
    [
        (
            {"id": "configure-1", "type": "configure", "provider": "fake"},
            "fake",
            None,
        ),
        (
            {"id": "configure-1", "type": "configure", "model": "configured-model"},
            "fake",
            "configured-model",
        ),
        (
            {
                "id": "configure-1",
                "type": "configure",
                "provider": "fake",
                "model": "configured-model",
            },
            "fake",
            "configured-model",
        ),
    ],
)
def test_rpc_trusted_rebuild_preserves_configure_overrides(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    configure_command: dict[str, object],
    expected_provider: str,
    expected_model: str | None,
) -> None:
    # The startup config is intentionally untrusted, but WISP_TRUST=1 makes the lazy
    # RPC gate approve trust before the first prompt. A configure command that arrives
    # before that prompt must outrank the trusted project's provider/model defaults.
    from wisp.cli import rpc
    from wisp.config import WispConfig

    project_auth = tmp_path / "project-auth.json"
    (tmp_path / ".wisp").mkdir()
    (tmp_path / ".wisp" / "settings.json").write_text(
        json.dumps(
            {
                "provider": "fake",
                "model": "project-model",
                "auth_path": str(project_auth),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WISP_PROVIDER", "fake")
    monkeypatch.setenv("WISP_MODEL", "")
    monkeypatch.setenv("WISP_TRUST", "1")

    session_dir = tmp_path / "sessions"
    config = WispConfig.from_env(session_dir=session_dir, trusted=False)

    async def fake_read_rpc_stdin(send: Any, _stop_reader: Any) -> None:
        async with send:
            await send.send(rpc._RpcInputCommand(configure_command))
            await send.send(rpc._RpcInputCommand({"id": "p1", "type": "prompt", "prompt": "hello"}))
            await send.send(rpc._RpcInputClosed())

    monkeypatch.setattr(rpc, "_read_rpc_stdin", fake_read_rpc_stdin)

    async def scenario() -> list[dict[str, object]]:
        output = io.StringIO()
        with redirect_stdout(output):
            await rpc._run_rpc(
                config,
                startup_trusted=False,
                config_overrides=rpc._ConfigOverrides(session_dir=session_dir),
            )
        return _jsonl_records(output.getvalue())

    records = anyio.run(scenario)
    applied = next(record for record in records if record["type"] == "project.config.applied")

    assert applied["provider"] == expected_provider
    assert applied["model"] == expected_model
    assert applied["auth_path"] == str(project_auth)


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
    trust_file = tmp_path / "trust.json"

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"p1","type":"prompt","prompt":"hello"}\n',
        env={
            "WISP_PROVIDER": "fake",
            "WISP_MODEL": "",
            "WISP_TRUST_FILE": str(trust_file),
        },
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    resolved = [r for r in records if r["type"] == "trust.resolved"]
    assert resolved and resolved[0]["trusted"] is False
    # The forced-untrusted decision from input close is not persisted, so a later
    # interactive run still prompts.
    from wisp.trust import is_trusted

    assert is_trusted(Path.cwd(), trust_path=trust_file) is None


def test_rpc_trust_denial_with_reason_persists(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    from wisp.cli.rpc import _RpcTrustGate
    from wisp.trust import is_trusted

    project = tmp_path / "proj"
    project.mkdir()
    trust_file = tmp_path / "trust.json"
    monkeypatch.setenv("WISP_TRUST_FILE", str(trust_file))
    gate = _RpcTrustGate(project)

    async def scenario() -> bool:
        decision: bool | None = None
        done = anyio.Event()

        async def resolve() -> None:
            nonlocal decision
            decision = await gate.resolve()
            done.set()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(resolve)
            while gate._pending is None:
                await anyio.sleep(0)
            assert gate.resolve_request(
                request_id=gate._pending.request_id,
                trusted=False,
                reason="user declined",
            )
            await done.wait()
            task_group.cancel_scope.cancel()
        assert decision is not None
        return decision

    assert anyio.run(scenario) is False
    assert is_trusted(project, trust_path=trust_file) is False


def test_rpc_trust_transient_denial_with_reason_does_not_persist(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from wisp.cli.rpc import _RpcTrustGate
    from wisp.trust import is_trusted

    project = tmp_path / "proj"
    project.mkdir()
    trust_file = tmp_path / "trust.json"
    monkeypatch.setenv("WISP_TRUST_FILE", str(trust_file))
    gate = _RpcTrustGate(project)

    async def scenario() -> bool:
        decision: bool | None = None
        done = anyio.Event()

        async def resolve() -> None:
            nonlocal decision
            decision = await gate.resolve()
            done.set()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(resolve)
            while gate._pending is None:
                await anyio.sleep(0)
            assert gate.resolve_request(
                request_id=gate._pending.request_id,
                trusted=False,
                reason="Trust prompt closed",
                transient=True,
            )
            await done.wait()
            task_group.cancel_scope.cancel()
        assert decision is not None
        return decision

    assert anyio.run(scenario) is False
    assert is_trusted(project, trust_path=trust_file) is None


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


def test_rpc_gate_propagates_rebuild_error(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    # If the first-run rebuild fails (e.g. a trusted project's settings.json names an
    # unknown provider), the error must propagate OUT of resolve() so the prompt task's
    # handler can report it as a normal rpc.command.finished failure — not escape and
    # tear down the RPC process with a silent stream end.
    from wisp.cli.rpc import _RpcTrustGate
    from wisp.runtime.registry import UnknownProviderError

    monkeypatch.setenv("WISP_TRUST", "1")

    async def on_trusted() -> None:
        raise UnknownProviderError("no-such-provider")

    gate = _RpcTrustGate(Path.cwd(), on_first_trusted=on_trusted)

    async def scenario() -> None:
        with pytest.raises(UnknownProviderError):
            await gate.resolve()

    anyio.run(scenario)


def test_rpc_gate_does_not_cache_after_failed_rebuild(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # A failed rebuild must NOT cache the decision: otherwise the caller reports this
    # prompt as failed, but the NEXT prompt returns the cached trust, skips the rebuild,
    # and silently runs the stale untrusted startup provider. Each prompt must re-attempt
    # the rebuild and fail the same way instead.
    from wisp.cli.rpc import _RpcTrustGate
    from wisp.runtime.registry import UnknownProviderError

    monkeypatch.setenv("WISP_TRUST", "1")
    attempts = 0

    async def on_trusted() -> None:
        nonlocal attempts
        attempts += 1
        raise UnknownProviderError("no-such-provider")

    gate = _RpcTrustGate(Path.cwd(), on_first_trusted=on_trusted)

    async def scenario() -> None:
        with pytest.raises(UnknownProviderError):
            await gate.resolve()
        assert gate._resolved is False  # not cached — a retry is possible
        # A second prompt re-attempts the rebuild (and fails again) rather than
        # returning a cached "trusted" and running stale config.
        with pytest.raises(UnknownProviderError):
            await gate.resolve()
        assert attempts == 2

    anyio.run(scenario)


def test_trusted_rebuild_preserves_event_bus_identity() -> None:
    # Regression: the first-run rebuild adopts the trusted runtime's PROVIDERS only
    # (via dataclasses.replace), NOT the whole runtime. A rebuilt runtime has its own
    # fresh EventBus/ExtensionAPI; wholesale reassignment would leave the RPC loop's
    # runtime on a different bus than the one the already-built agent emits on,
    # splitting the event stream. This asserts the swap the rebuild performs keeps
    # events/api/tools shared while switching providers.
    from dataclasses import replace

    from wisp.runtime.extensions import build_runtime

    async def scenario() -> None:
        original = await build_runtime()
        trusted = await build_runtime()
        assert original.events is not trusted.events  # premise: distinct buses

        rebuilt = replace(original, providers=trusted.providers)

        assert rebuilt.events is original.events  # agent's bus preserved
        assert rebuilt.api is original.api
        assert rebuilt.tools is original.tools
        assert rebuilt.providers is trusted.providers  # trusted providers adopted

    anyio.run(scenario)


def test_rpc_run_uses_replace_for_trusted_runtime_swap() -> None:
    # Guard against a regression back to wholesale `runtime = trusted_runtime`, which
    # would reintroduce the event-bus split. The rebuild must swap providers via
    # replace(...) and must not reassign the whole runtime.
    import inspect

    from wisp.cli import rpc

    src = inspect.getsource(rpc._run_rpc)
    assert "replace(runtime, providers=trusted_runtime.providers)" in src
    assert "runtime = trusted_runtime" not in src  # no wholesale swap


def test_rpc_prompt_command_reports_rebuild_provider_error(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # End-to-end through _run_rpc_prompt_command: a resolve() that raises
    # UnknownProviderError (the rebuild's failure mode) is caught and surfaced as an
    # rpc.command.finished with ok=false + error, never a silent process teardown.
    import io
    from contextlib import redirect_stdout

    from wisp.cli.rpc import _RpcTrustGate, _run_rpc_prompt_command
    from wisp.runtime.registry import UnknownProviderError
    from wisp.sessions.jsonl import JsonlSessionStore

    async def on_trusted() -> None:
        raise UnknownProviderError("no-such-provider")

    monkeypatch.setenv("WISP_TRUST", "1")
    gate = _RpcTrustGate(Path.cwd(), on_first_trusted=on_trusted)
    sessions = JsonlSessionStore(tmp_path)
    session = sessions.create()

    class _Agent:
        trusted = False

        def run(self, *a: object, **k: object) -> object:  # pragma: no cover - never reached
            raise AssertionError("agent.run must not be reached when the rebuild fails")

    async def scenario() -> list[dict[str, object]]:
        send, receive = anyio.create_memory_object_stream[Any](10)
        buf = io.StringIO()
        with redirect_stdout(buf):
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    cast(Any, _run_rpc_prompt_command),
                    _Agent(),
                    session,
                    (),
                    0,
                    "hello",
                    "p1",
                    "prompt",
                    anyio.CancelScope(),
                    send.clone(),
                    gate,
                )
                async with receive:
                    async for _ in receive:
                        break
        return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]

    records = anyio.run(scenario)
    finished = [r for r in records if r.get("type") == "rpc.command.finished"]
    assert finished, f"no rpc.command.finished emitted; got {[r.get('type') for r in records]}"
    assert finished[0]["ok"] is False
    error = finished[0].get("error")
    assert isinstance(error, str)
    assert "no-such-provider" in error


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
