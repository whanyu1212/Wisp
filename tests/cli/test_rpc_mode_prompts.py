# ruff: noqa: F403,F405

from __future__ import annotations

from collections.abc import Mapping

import pytest

import wisp.coding.tool_execution as tool_execution
from tests.support.cli import *
from tests.support.rpc import guard_rpc_command_serialization


def test_rpc_mode_accepts_zero_tool_iteration_cap(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--max-tool-iterations", "0", "--session-dir", str(tmp_path)],
        input='{"id":"shutdown-1","type":"shutdown"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "rpc.command.finished",
    ]
    assert records[1]["ok"] is True


def test_rpc_mode_runs_prompt_commands_with_explicit_id(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"cmd-1","type":"prompt","prompt":"hello"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "agent.started",
        "turn.started",
        "context.estimated",
        "message.started",
        "message.delta",
        "message.completed",
        "turn.completed",
        "session.saved",
        "agent.completed",
        "rpc.command.finished",
    ]
    assert all("schema_version" not in record for record in records)
    assert records[0]["type"] == "rpc.command.started"
    assert records[0]["command_id"] == "cmd-1"
    assert records[0]["command_type"] == "prompt"
    assert records[5]["delta"] == "fake response to: hello"
    assert records[6]["content"] == "fake response to: hello"
    assert records[-1]["command_id"] == "cmd-1"
    assert records[-1]["command_type"] == "prompt"
    assert records[-1]["ok"] is True
    assert records[-1]["error"] is None


def test_rpc_mode_init_refuses_existing_project_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    guidance = project / "AGENTS.md"
    guidance.write_text("Keep me.\n", encoding="utf-8")
    monkeypatch.chdir(project)

    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path / "sessions")],
        input='{"id":"init-1","type":"init"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "error",
        "rpc.command.finished",
    ]
    assert records[1]["message"] == f"Project guidance already exists: {guidance}"
    assert records[2]["command_type"] == "init"
    assert records[2]["ok"] is False
    assert guidance.read_text(encoding="utf-8") == "Keep me.\n"


def test_rpc_mode_generates_command_id_when_missing(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"type":"prompt","prompt":"hello"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    started = records[0]
    finished = records[-1]
    assert started["type"] == "rpc.command.started"
    assert finished["type"] == "rpc.command.finished"
    assert isinstance(started["command_id"], str)
    assert started["command_id"]
    assert finished["command_id"] == started["command_id"]
    assert finished["ok"] is True


def test_rpc_mode_runs_multiple_prompt_commands_in_one_session(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"cmd-1","type":"prompt","prompt":"first"}\n'
            '{"id":"cmd-2","type":"prompt","prompt":"second"}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assistant_messages = [
        record["content"] for record in records if record["type"] == "message.completed"
    ]
    assert assistant_messages == ["fake response to: first", "fake response to: second"]
    started = [record for record in records if record["type"] == "rpc.command.started"]
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [record["command_id"] for record in started] == ["cmd-1", "cmd-2"]
    assert [record["command_id"] for record in finished] == ["cmd-1", "cmd-2"]
    assert all(record["ok"] is True for record in finished)
    session_paths = {record["path"] for record in records if record["type"] == "session.saved"}
    assert len(session_paths) == 1
    session_file = next(tmp_path.glob("*.jsonl"))
    session_records = _jsonl_records(session_file.read_text(encoding="utf-8"))
    user_messages = [
        record["message"]["content"]
        for record in session_records
        if record["kind"] == "message" and record["message"]["role"] == "user"
    ]
    assert user_messages == ["first", "second"]


def test_rpc_mode_reports_bad_commands_and_continues(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            "not json\n"
            "[]\n"
            '{"id":"bad","type":"missing"}\n'
            '{"id":"missing-prompt","type":"prompt"}\n'
            '{"id":123,"type":"prompt","prompt":"bad id"}\n'
            '{"id":"ok","type":"prompt","prompt":"ok"}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    records = _jsonl_records(result.stdout)
    error_messages = [record["message"] for record in records if record["type"] == "error"]
    assert error_messages[:5] == [
        "RPC frame is not valid JSON",
        "RPC frame must be a JSON object",
        "Unknown RPC command: missing",
        "RPC command does not match the negotiated schema",
        "RPC command does not match the negotiated schema",
    ]
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert (finished[0]["command_id"], finished[0]["ok"], finished[0]["error"]) == (
        "bad",
        False,
        "Unknown RPC command: missing",
    )
    assert any(
        record["type"] == "message.completed" and record["content"] == "fake response to: ok"
        for record in records
    )
    assert finished[-1]["command_id"] == "ok"
    assert finished[-1]["ok"] is True


def test_rpc_mode_reports_internal_tool_result_failure_and_continues(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    original_summary = tool_execution.summarize_tool_result
    summary_calls = 0

    def fail_first_summary(
        name: str,
        data: Mapping[str, object],
        *,
        truncated: bool = False,
    ) -> str | None:
        nonlocal summary_calls
        summary_calls += 1
        if summary_calls == 1:
            raise RuntimeError("internal api-key=secret")
        return original_summary(name, data, truncated=truncated)

    monkeypatch.setattr(rpc_host_module, "build_runtime", build_tool_runtime)
    monkeypatch.setattr(tool_execution, "summarize_tool_result", fail_first_summary)

    result = runner.invoke(
        app,
        [
            "--mode",
            "rpc",
            "--yes",
            "--allow-tool",
            "danger",
            "--session-dir",
            str(tmp_path),
        ],
        input=(
            '{"id":"cmd-1","type":"prompt","prompt":"first"}\n'
            '{"id":"cmd-2","type":"prompt","prompt":"second"}\n'
        ),
        env={"WISP_PROVIDER": "tool-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert "internal api-key=secret" not in result.stdout
    records = _jsonl_records(result.stdout)
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [(record["command_id"], record["ok"], record["error"]) for record in finished] == [
        ("cmd-1", False, "Internal error while processing a tool result"),
        ("cmd-2", True, None),
    ]
    assert any(
        record["type"] == "message.completed" and record["content"] == "done" for record in records
    )


def test_rpc_mode_preserves_unknown_command_lifecycle(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"future-1","type":"future_command","payload":"kept"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "error",
        "rpc.command.finished",
    ]
    assert records[0]["command_id"] == "future-1"
    assert records[0]["command_type"] == "future_command"
    assert records[1]["message"] == "Unknown RPC command: future_command"
    assert records[2]["command_id"] == "future-1"
    assert records[2]["command_type"] == "future_command"
    assert records[2]["ok"] is False
    assert records[2]["error"] == "Unknown RPC command: future_command"


def test_rpc_mode_finishes_command_when_session_write_fails_before_file_exists(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()

    async def fail_append_message(
        self: JsonlSession,
        message: Message,
        *,
        operation_id: str | None = None,
    ) -> object:
        del operation_id
        raise RuntimeError("session write failed")

    monkeypatch.setattr(JsonlSession, "append_message", fail_append_message)

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"cmd-1","type":"prompt","prompt":"hello"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert {record["type"] for record in records} >= {"error", "rpc.command.finished"}
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [
        (record["command_id"], record["command_type"], record["ok"], record["error"])
        for record in finished
    ] == [("cmd-1", "prompt", False, "session write failed")]


def test_rpc_mode_preserves_failed_prompt_in_next_prompt_history(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(rpc_host_module, "build_runtime", build_failing_runtime)

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"cmd-1","type":"prompt","prompt":"fail"}\n'
            '{"id":"cmd-2","type":"prompt","prompt":"second"}\n'
        ),
        env={"WISP_PROVIDER": "failing-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    completions = [
        (record["content"], record["finish_reason"])
        for record in records
        if record["type"] == "message.completed"
    ]
    # The failed prompt's opened message closes as an empty error completion.
    assert completions == [("", "error"), ("saw failed history", "stop")]
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [(record["command_id"], record["ok"]) for record in finished] == [
        ("cmd-1", False),
        ("cmd-2", True),
    ]


def test_rpc_mode_excludes_cancelled_prompt_from_next_prompt_history(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(rpc_host_module, "build_runtime", build_cancellable_runtime)

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"cmd-1","type":"prompt","prompt":"slow"}\n'
            '{"id":"cmd-2","type":"prompt","prompt":"second"}\n'
            '{"id":"cancel-1","type":"cancel","target_id":"cmd-1"}\n'
        ),
        env={"WISP_PROVIDER": "cancellable-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assistant_messages = [
        record["content"] for record in records if record["type"] == "message.completed"
    ]
    assert assistant_messages == ["done second"]
    session = JsonlSessionStore(tmp_path).latest()
    persisted_user_messages = [
        message.content for message in session.read_messages() if message.role == "user"
    ]
    assert persisted_user_messages == ["second"]


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)
