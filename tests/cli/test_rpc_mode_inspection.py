# ruff: noqa: F403,F405

from __future__ import annotations

import pytest

from tests.support.cli import *
from tests.support.rpc import guard_rpc_command_serialization


def test_rpc_mode_reports_commands_before_prompt(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"commands-1","type":"get_commands"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "rpc.commands",
        "rpc.command.finished",
    ]
    assert all("schema_version" not in record for record in records)
    report = records[1]
    assert report["command_id"] == "commands-1"
    assert [command["name"] for command in report["commands"]] == [
        "help",
        "init",
        "queue",
        "compact",
        "context",
        "search",
        "history",
        "update",
        "skills",
        "mcp",
        "permissions",
        "plan",
        "build",
        "model",
        "new",
        "resume",
        "name",
        "clone",
        "tree",
        "unrevert",
        "provider",
        "auth",
        "connect",
        "disconnect",
        "quit",
    ]
    compact = next(command for command in report["commands"] if command["name"] == "compact")
    assert compact["slash_command"] == "/compact"
    assert compact["category"] == "session"
    assert compact["arguments"] == [
        {
            "name": "instructions",
            "description": "Optional compaction guidance",
            "required": False,
        }
    ]
    assert records[-1]["command_id"] == "commands-1"
    assert records[-1]["command_type"] == "get_commands"


def test_rpc_mode_reports_active_skill_catalog(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"skills-1","type":"get_skills"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "rpc.skills",
        "rpc.command.finished",
    ]
    report = records[1]
    assert "schema_version" not in report
    assert report["command_id"] == "skills-1"
    assert report["catalog"] == {
        "entries": [
            {
                "name": "github-pr-delivery",
                "description": (
                    "Deliver focused changes through a GitHub pull request with verified commits, "
                    "current-head CI, thread-aware review, and explicit authorization boundaries."
                ),
                "source": "package:wisp",
            },
            {
                "name": "wisp-development",
                "description": (
                    "Develop Wisp and its current extension surfaces while preserving "
                    "architecture and safety boundaries."
                ),
                "source": "package:wisp",
            },
        ],
        "diagnostics": [],
        "project_trusted": False,
    }
    assert records[-1]["command_type"] == "get_skills"
    assert records[-1]["ok"] is True


def test_rpc_mode_reports_mcp_status(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"mcp-1","type":"get_mcp_status"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "rpc.mcp",
        "rpc.command.finished",
    ]
    report = records[1]
    assert "schema_version" not in report
    assert report["command_id"] == "mcp-1"
    assert report["status"] == {"servers": []}
    assert records[-1]["command_type"] == "get_mcp_status"
    assert records[-1]["ok"] is True


def test_rpc_mode_reports_stats_after_queued_prompt(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"prompt-1","type":"prompt","prompt":"hello"}\n'
            '{"id":"stats-1","type":"get_session_stats"}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    stats = next(record for record in records if record["type"] == "session.stats")
    assert stats["command_id"] == "stats-1"
    assert stats["stats"]["session_id"]
    assert stats["stats"]["entry_count"] == 5
    assert stats["stats"]["active_message_count"] == 2
    assert stats["stats"]["context"]["estimate"]["total_tokens"] > 0
    finished = [
        record
        for record in records
        if record["type"] == "rpc.command.finished" and record["command_id"] == "stats-1"
    ]
    assert finished == [
        {
            "type": "rpc.command.finished",
            "timestamp": finished[0]["timestamp"],
            "command_id": "stats-1",
            "command_type": "get_session_stats",
            "ok": True,
            "error": None,
        }
    ]


def test_rpc_mode_reports_messages_after_queued_prompt(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"prompt-1","type":"prompt","prompt":"hello"}\n'
            '{"id":"messages-1","type":"get_messages","limit":5}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    report = next(record for record in records if record["type"] == "rpc.messages")
    assert report["command_id"] == "messages-1"
    assert "schema_version" not in report
    assert report["session_id"]
    assert report["session_path"]
    assert report["active_leaf_id"]
    assert report["truncated"] is False
    assert report["next_before_entry_id"] is None
    turn_messages = report["messages"][-2:]
    assert [message["role"] for message in turn_messages] == ["user", "assistant"]
    assert [message["content"] for message in turn_messages] == [
        "hello",
        "fake response to: hello",
    ]
    assert turn_messages[1]["parent_id"] == turn_messages[0]["entry_id"]
    assert turn_messages[1]["entry_id"] == report["active_leaf_id"]
    assert all(message["content_truncated"] is False for message in turn_messages)
    finished = [
        record
        for record in records
        if record["type"] == "rpc.command.finished" and record["command_id"] == "messages-1"
    ]
    assert finished == [
        {
            "type": "rpc.command.finished",
            "timestamp": finished[0]["timestamp"],
            "command_id": "messages-1",
            "command_type": "get_messages",
            "ok": True,
            "error": None,
        }
    ]


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)
