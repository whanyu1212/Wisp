# ruff: noqa: F403,F405

from __future__ import annotations

from tests.cli_support import *


def test_json_mode_outputs_events_as_jsonl(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-p", "hello", "--mode", "json", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "agent.started",
        "turn.started",
        "context.estimated",
        "message.started",
        "message.delta",
        "message.delta",
        "message.delta",
        "message.delta",
        "message.completed",
        "turn.completed",
        "session.saved",
        "agent.completed",
    ]
    assert all(record["schema_version"] == 21 for record in records)
    assert "timestamp" in records[0]
    assert "session_id" in records[0]
    assert "fake response to: hello" == "".join(
        str(record["delta"]) for record in records if record["type"] == "message.delta"
    )
    assert str(records[-2]["path"]).endswith(".jsonl")
    assert records[-1]["outcome"] == "completed"


def test_json_mode_outputs_tool_events_as_jsonl(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module.rpc, "build_runtime", build_tool_runtime)

    result = runner.invoke(
        app,
        [
            "-p",
            "use tool",
            "--mode",
            "json",
            "--allow-tool",
            "danger",
            "--yes",
            "--session-dir",
            str(tmp_path),
        ],
        env={"WISP_PROVIDER": "tool-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    records = _jsonl_records(result.stdout)
    types = [record["type"] for record in records]
    assert "tool.execution.started" in types
    assert "tool.call" in types
    assert "tool.approval.requested" not in types
    assert "tool.approval.resolved" not in types
    assert "tool.execution.ended" in types
    assert "tool.result" in types
    assert records[types.index("tool.call")]["arguments"] == {"path": "file.txt"}
    assert records[types.index("tool.result")]["output"] == "changed file.txt"
    assert (
        records[types.index("message.completed", types.index("tool.result"))]["content"] == "done"
    )


def test_json_mode_validation_errors_are_jsonl(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "-p",
            "hello",
            "--mode",
            "json",
            "--max-tool-iterations",
            "-1",
            "--session-dir",
            str(tmp_path),
        ],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1, result.output
    assert result.stderr == ""
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == ["error"]
    assert records[0]["message"] == "--max-tool-iterations must be non-negative"


def test_json_mode_invalid_auto_compaction_env_is_structured(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["-p", "hello", "--mode", "json", "--session-dir", str(tmp_path)],
        env={
            "WISP_PROVIDER": "fake",
            "WISP_AUTO_COMPACTION": "sometimes",
            "WISP_TRUST": "1",
        },
    )

    assert result.exit_code == 1
    assert "Traceback" not in result.stdout
    records = _jsonl_records(result.stdout)
    assert len(records) == 1
    assert records[0]["type"] == "error"
    assert "WISP_AUTO_COMPACTION must be one of" in records[0]["message"]


def test_json_mode_emits_error_event_without_stderr_noise(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module.rpc, "build_runtime", build_tool_runtime)

    result = runner.invoke(
        app,
        [
            "-p",
            "use tool",
            "--mode",
            "json",
            "--allow-tool",
            "danger",
            "--yes",
            "--max-tool-iterations",
            "0",
            "--session-dir",
            str(tmp_path),
        ],
        env={"WISP_PROVIDER": "tool-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1, result.output
    assert result.stderr == ""
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "agent.started",
        "turn.started",
        "context.estimated",
        "message.started",
        "message.completed",
        "error",
        "turn.completed",
        "agent.completed",
    ]
    assert records[-3]["message"] == "Maximum tool iterations exceeded: 0"
    assert records[-2]["outcome"] == "failed"
    assert records[-1]["outcome"] == "failed"
