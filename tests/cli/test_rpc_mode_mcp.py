"""RPC-mode tests for changing and restarting MCP servers without restarting Wisp."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# The support CliRunner performs the RPC handshake before sending commands.
from tests.support.cli import CliRunner, _jsonl_records
from tests.support.paths import FIXTURES_DIR
from wisp.cli import app
from wisp.config.settings import user_settings_path

SECRET = "sentinel-mcp-secret"


def _run_rpc(tmp_path: Path, commands: list[dict[str, object]]) -> list[dict[str, object]]:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path / "sessions")],
        input="".join(json.dumps(command) + "\n" for command in commands),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )
    assert result.exit_code == 0, result.output
    assert SECRET not in result.output
    return _jsonl_records(result.stdout)


def _reports(records: list[dict[str, object]]) -> dict[str, object]:
    return {
        str(record["command_id"]): record["status"]
        for record in records
        if record["type"] == "rpc.mcp"
    }


def _finished(records: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {
        str(record["command_id"]): record
        for record in records
        if record["type"] == "rpc.command.finished"
    }


def _saved_servers() -> object:
    path = user_settings_path()
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("mcp_servers")


@pytest.mark.process
def test_add_disable_enable_and_remove_apply_without_restart(tmp_path: Path) -> None:
    closed_file = tmp_path / "fixture-closed"
    add = {
        "id": "add",
        "type": "add_mcp_server",
        "name": "fixture",
        "command": sys.executable,
        "args": ["-u", str(FIXTURES_DIR / "mcp_stdio_server.py")],
        "env": {"WISP_MCP_TEST_CLOSED_FILE": str(closed_file), "WISP_MCP_TEST_LITERAL": SECRET},
    }
    records = _run_rpc(
        tmp_path,
        [
            add,
            {
                "id": "disable",
                "type": "set_mcp_server_enabled",
                "name": "fixture",
                "enabled": False,
            },
            {"id": "enable", "type": "set_mcp_server_enabled", "name": "fixture", "enabled": True},
            {"id": "reconnect", "type": "reconnect_mcp"},
            {"id": "remove", "type": "remove_mcp_server", "name": "fixture"},
        ],
    )

    connected = {
        "servers": [
            {
                "name": "fixture",
                "status": "connected",
                "tool_names": ["mcp__fixture__echo", "mcp__fixture__runtime_environment"],
                "error": None,
            }
        ]
    }
    assert _reports(records) == {
        "add": connected,
        "disable": {
            "servers": [{"name": "fixture", "status": "disabled", "tool_names": [], "error": None}]
        },
        "enable": connected,
        "reconnect": connected,
        "remove": {"servers": []},
    }
    assert all(record["ok"] for record in _finished(records).values())
    # Each restart stopped the previous process instead of leaving it running.
    assert closed_file.read_text(encoding="utf-8") == "closed"
    assert _saved_servers() is None


def test_invalid_server_is_refused_without_echoing_values(tmp_path: Path) -> None:
    records = _run_rpc(
        tmp_path,
        [
            {
                "id": "bad-name",
                "type": "add_mcp_server",
                "name": "Bad Name",
                "command": "npx",
                "env": {"TOKEN": SECRET},
            },
            {
                "id": "bad-env",
                "type": "add_mcp_server",
                "name": "fixture",
                "command": "npx",
                "env": {"TOKEN": SECRET, "OTHER": 7},
            },
            {
                "id": "both",
                "type": "add_mcp_server",
                "name": "x",
                "command": "npx",
                "url": "https://e.com",
            },
        ],
    )

    finished = _finished(records)
    assert {name: record["ok"] for name, record in finished.items()} == {
        "bad-name": False,
        "both": False,
    }
    assert "is invalid" in str(finished["bad-name"]["error"])
    # A non-string env value fails the wire schema before the command is accepted.
    assert {"type": "error", "message": "RPC command does not match the negotiated schema"} in [
        {key: record[key] for key in ("type", "message")}
        for record in records
        if record["type"] == "error"
    ]
    assert _saved_servers() is None


def test_changing_a_missing_server_fails_and_reconnect_with_none_configured(tmp_path: Path) -> None:
    records = _run_rpc(
        tmp_path,
        [
            {"id": "remove", "type": "remove_mcp_server", "name": "nope"},
            {"id": "reconnect", "type": "reconnect_mcp"},
        ],
    )

    finished = _finished(records)
    assert finished["remove"]["ok"] is False
    assert "no MCP server named 'nope'" in str(finished["remove"]["error"])
    assert finished["reconnect"]["ok"] is True
    assert _reports(records) == {"reconnect": {"servers": []}}


def test_mcp_command_waits_for_a_running_prompt(tmp_path: Path) -> None:
    records = _run_rpc(
        tmp_path,
        [
            {"id": "prompt", "type": "prompt", "prompt": "hello"},
            {"id": "reconnect", "type": "reconnect_mcp"},
            {"id": "after", "type": "prompt", "prompt": "again"},
        ],
    )

    finished = [
        (record["command_id"], record["ok"])
        for record in records
        if record["type"] == "rpc.command.finished"
    ]
    assert finished == [("prompt", True), ("reconnect", True), ("after", True)]
