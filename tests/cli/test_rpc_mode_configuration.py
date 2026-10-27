# ruff: noqa: F403,F405

from __future__ import annotations

import pytest

from tests.support.cli import *
from tests.support.rpc import guard_rpc_command_serialization


def test_rpc_mode_reports_idle_and_reconfigured_state(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"state-1","type":"get_state"}\n'
            '{"id":"configure-1","type":"configure","model":"fake-custom-model","effort":"high"}\n'
            '{"id":"state-2","type":"get_state"}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    reports = [record for record in records if record["type"] == "rpc.state"]
    assert [record["command_id"] for record in reports] == ["state-1", "state-2"]
    assert reports[0]["state"] == {
        "provider": "fake",
        "model": "fake",
        "mode": "build",
        "effort": None,
        "auto_compaction_enabled": True,
        "web_search": False,
        "steering_mode": "one_at_a_time",
        "follow_up_mode": "one_at_a_time",
        "pending_steering_count": 0,
        "pending_follow_up_count": 0,
        "session_id": None,
        "session_path": None,
        "session_name": None,
        "active_command_id": None,
        "active_command_type": None,
        "cancel_requested": False,
    }
    assert reports[1]["state"] == {
        **reports[0]["state"],
        "model": "fake-custom-model",
        "effort": "high",
    }
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "rpc.state",
        "rpc.command.finished",
        "rpc.command.started",
        "rpc.model_catalog",
        "rpc.command.finished",
        "rpc.command.started",
        "rpc.state",
        "rpc.command.finished",
    ]


def test_rpc_mode_new_session_is_lazy_and_reports_no_selected_session(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"new-1","type":"new_session"}\n'
            '{"id":"new-2","type":"new_session"}\n'
            '{"id":"state-1","type":"get_state"}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [(record["command_id"], record["ok"]) for record in finished] == [
        ("new-1", True),
        ("new-2", True),
        ("state-1", True),
    ]
    state = next(record["state"] for record in records if record["type"] == "rpc.state")
    assert state["session_id"] is None
    assert state["session_path"] is None
    assert list(tmp_path.glob("*.jsonl")) == []


def test_rpc_mode_orders_provider_switch_between_prompts_in_same_session(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path / "sessions")],
        input=(
            '{"id":"prompt-1","type":"prompt","prompt":"first"}\n'
            '{"id":"configure-1","type":"configure","provider":"openai"}\n'
            '{"id":"prompt-2","type":"prompt","prompt":"second"}\n'
        ),
        env={
            "WISP_PROVIDER": "fake",
            "WISP_MODEL": "",
            "WISP_AUTH_FILE": str(tmp_path / "auth.json"),
            "OPENAI_API_KEY": "",
        },
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    started = [record for record in records if record["type"] == "rpc.command.started"]
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [record["command_id"] for record in started] == [
        "prompt-1",
        "configure-1",
        "prompt-2",
    ]
    assert [(record["command_id"], record["ok"]) for record in finished] == [
        ("prompt-1", True),
        ("configure-1", True),
        ("prompt-2", False),
    ]
    estimates = [record for record in records if record["type"] == "context.estimated"]
    assert [record["provider"] for record in estimates] == ["fake", "openai"]
    agent_starts = [record for record in records if record["type"] == "agent.started"]
    assert len(agent_starts) == 2
    assert agent_starts[0]["session_id"] == agent_starts[1]["session_id"]
    assert any(record.get("content") == "fake response to: first" for record in records)
    assert any(
        record["type"] == "error" and "openai credentials are required" in record["message"]
        for record in records
    )


def test_rpc_mode_configure_rejects_unknown_provider(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"configure-1","type":"configure","provider":"missing"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "error",
        "rpc.command.finished",
    ]
    assert records[1]["message"] == "Unknown provider: missing"
    assert records[2]["ok"] is False


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)
