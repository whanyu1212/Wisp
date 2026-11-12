"""Tests for the RPC backend connecting MCP servers after it starts serving."""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import anyio
import pytest

from tests.support.cli import _read_rpc_test_handshake
from tests.support.paths import FIXTURES_DIR
from wisp.cli import rpc
from wisp.config.mcp_servers import validated_mcp_server
from wisp.config.runtime import WispConfig
from wisp.mcp.config import McpServerConfig
from wisp.providers.events import ProviderResponseCompleted, ProviderResponseStarted
from wisp.providers.fake import ScriptedProvider
from wisp.rpc.commands import ParsedRpcCommand, RpcCommandAdapter
from wisp.rpc.configuration import _ConfigOverrides
from wisp.runtime.api import WispRuntime
from wisp.runtime.extensions import build_runtime

_FIXTURE_TOOLS = ["mcp__fixture__echo", "mcp__fixture__runtime_environment"]


def _fixture_server(tmp_path: Path) -> McpServerConfig:
    return validated_mcp_server(
        "fixture",
        {
            "command": sys.executable,
            "args": ["-u", str(FIXTURES_DIR / "mcp_stdio_server.py")],
            "env": {"WISP_MCP_TEST_CLOSED_FILE": str(tmp_path / "fixture-closed")},
        },
    )


def _answering_provider() -> ScriptedProvider:
    return ScriptedProvider(
        [[ProviderResponseStarted(model="scripted"), ProviderResponseCompleted(content="done")]]
    )


def _serve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: ScriptedProvider,
    commands: list[dict[str, object]],
    *,
    connect_first: bool = False,
    continue_latest: bool = False,
) -> list[dict[str, Any]]:
    """Run the RPC backend over ``commands`` with one MCP server left to connect.

    With ``connect_first`` the server is connected before the host starts instead,
    as it was before the backend deferred the connect. With ``continue_latest`` the
    backend resumes the session an earlier call in the same ``tmp_path`` saved.

    Input closes after the last command, so the backend exits once its running
    command ends, cancelling a connect that is still in progress.

    Returns:
        list[dict[str, Any]]: Every event the backend wrote, in order.
    """

    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    monkeypatch.chdir(project)
    monkeypatch.setenv("WISP_PROVIDER", "scripted")
    monkeypatch.setenv("WISP_MODEL", "")
    session_dir = tmp_path / "sessions"
    config = WispConfig.from_env(
        provider="scripted", session_dir=session_dir, project_dir=project, trusted=True
    )

    async def build_deferred_runtime(_config: WispConfig, *, connect_mcp: bool) -> WispRuntime:
        assert not connect_mcp
        runtime = await build_runtime(
            mcp_servers=(_fixture_server(tmp_path),), connect_mcp=connect_first
        )
        runtime.providers.register(provider)
        return runtime

    async def send_commands(send: Any, _stop_reader: Any, _write_event: Any) -> None:
        async with send:
            for payload in commands:
                command = RpcCommandAdapter.validate_python(payload)
                await send.send(
                    rpc._RpcInputCommand(ParsedRpcCommand.from_known(command, payload=payload))
                )
            await send.send(rpc._RpcInputClosed())

    monkeypatch.setattr(rpc, "build_session_runtime", build_deferred_runtime)
    monkeypatch.setattr(rpc, "_read_rpc_stdin", send_commands)
    monkeypatch.setattr(rpc._rpc_transport, "read_rpc_stdin_handshake", _read_rpc_test_handshake)
    output = io.StringIO()

    async def scenario() -> None:
        with redirect_stdout(output):
            await rpc._run_rpc(
                config,
                all_tools=True,
                continue_latest=continue_latest,
                startup_trusted=True,
                config_overrides=_ConfigOverrides(provider="scripted", session_dir=session_dir),
                project_context_root=project,
            )

    anyio.run(scenario)
    return [json.loads(line) for line in output.getvalue().splitlines() if line]


def _connect_never_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    from wisp.mcp.runtime import McpRuntime

    async def start(cls: type[McpRuntime], *_args: object, **_kwargs: object) -> McpRuntime:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    monkeypatch.setattr(McpRuntime, "start", classmethod(start))


def _finished(records: list[dict[str, Any]], command_id: str) -> dict[str, Any]:
    return next(
        record
        for record in records
        if record["type"] == "rpc.command.finished" and record["command_id"] == command_id
    )


@pytest.mark.process
def test_first_prompt_waits_for_mcp_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _answering_provider()

    records = _serve(
        tmp_path, monkeypatch, provider, [{"id": "p1", "type": "prompt", "prompt": "hi"}]
    )

    assert _finished(records, "p1")["ok"]
    # The prompt arrived before the server connected, yet the model was offered its tools.
    assert [tool.name for tool in provider.calls[0].tools][-2:] == _FIXTURE_TOOLS
    types = [record["type"] for record in records]
    assert types.index("mcp.status.updated") < types.index("agent.started")
    updated = records[types.index("mcp.status.updated")]["status"]["servers"]
    assert [(server["status"], server["tool_names"]) for server in updated] == [
        ("connected", _FIXTURE_TOOLS)
    ]
    assert (tmp_path / "fixture-closed").read_text(encoding="utf-8") == "closed"


@pytest.mark.process
def test_first_request_matches_an_eager_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Connecting after the host starts sends the model the same tools and instructions."""

    prompt: list[dict[str, object]] = [{"id": "p1", "type": "prompt", "prompt": "hi"}]
    eager = _answering_provider()
    deferred = _answering_provider()

    _serve(tmp_path, monkeypatch, eager, prompt, connect_first=True)
    _serve(tmp_path, monkeypatch, deferred, prompt)

    assert deferred.calls[0].tools == eager.calls[0].tools
    assert [message.name for message in eager.calls[0].tools][-2:] == _FIXTURE_TOOLS

    def instructions(provider: ScriptedProvider) -> list[str]:
        return [m.content for m in provider.calls[0].messages if m.role == "system"]

    assert instructions(deferred) == instructions(eager)


@pytest.mark.process
def test_compaction_waits_for_the_connect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Compaction holds the session, which the connect could not then hand its tools."""

    prompt: list[dict[str, object]] = [{"id": "p1", "type": "prompt", "prompt": "hi"}]
    _serve(tmp_path, monkeypatch, _answering_provider(), prompt, connect_first=True)

    records = _serve(
        tmp_path,
        monkeypatch,
        _answering_provider(),
        [{"id": "c1", "type": "compact"}],
        continue_latest=True,
    )

    types = [record["type"] for record in records]
    assert types.index("mcp.status.updated") < records.index(_finished(records, "c1"))
    updated = records[types.index("mcp.status.updated")]["status"]["servers"]
    assert [server["status"] for server in updated] == ["connected"]


def test_reads_are_served_while_connecting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _connect_never_ends(monkeypatch)

    records = _serve(
        tmp_path,
        monkeypatch,
        _answering_provider(),
        [{"id": "status", "type": "get_mcp_status"}, {"id": "page", "type": "get_messages"}],
    )

    assert _finished(records, "status")["ok"]
    assert _finished(records, "page")["ok"]
    status = next(record for record in records if record["type"] == "rpc.mcp")
    assert [(server["name"], server["status"]) for server in status["status"]["servers"]] == [
        ("fixture", "connecting")
    ]
    # The backend exited with the connect still running, and reported no result for it.
    assert "mcp.status.updated" not in [record["type"] for record in records]


def test_held_prompt_can_be_cancelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _connect_never_ends(monkeypatch)
    provider = _answering_provider()

    records = _serve(
        tmp_path,
        monkeypatch,
        provider,
        [
            {"id": "p1", "type": "prompt", "prompt": "hi"},
            {"id": "c1", "type": "cancel", "target_id": "p1"},
        ],
    )

    finished = _finished(records, "p1")
    assert not finished["ok"]
    assert finished["error"] == "RPC command cancelled: p1"
    assert provider.calls == []


def test_failed_connect_releases_the_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wisp.mcp.runtime import McpRuntime

    async def start(cls: type[McpRuntime], *_args: object, **_kwargs: object) -> McpRuntime:
        raise RuntimeError("Failed to initialize MCP runtime")

    monkeypatch.setattr(McpRuntime, "start", classmethod(start))
    provider = _answering_provider()

    records = _serve(
        tmp_path, monkeypatch, provider, [{"id": "p1", "type": "prompt", "prompt": "hi"}]
    )

    assert _finished(records, "p1")["ok"]
    assert all(not tool.name.startswith("mcp__") for tool in provider.calls[0].tools)
    errors = [record["message"] for record in records if record["type"] == "error"]
    assert errors == ["MCP servers could not be started; their tools are unavailable"]
    updated = next(record for record in records if record["type"] == "mcp.status.updated")
    assert [server["status"] for server in updated["status"]["servers"]] == ["unavailable"]
