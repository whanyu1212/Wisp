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
        "token.delta",
        "token.delta",
        "token.delta",
        "token.delta",
        "assistant.message",
        "session.saved",
    ]
    assert "timestamp" in records[0]
    assert "session_id" in records[0]
    assert "fake response to: hello" == "".join(
        str(record["delta"]) for record in records if record["type"] == "token.delta"
    )
    assert str(records[-1]["path"]).endswith(".jsonl")


def test_json_mode_outputs_tool_events_as_jsonl(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module, "build_runtime", build_tool_runtime)

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
    assert "tool.approval.requested" in types
    assert "tool.approval.resolved" in types
    assert "tool.execution.ended" in types
    assert "tool.result" in types
    assert records[types.index("tool.call")]["arguments"] == {"path": "file.txt"}
    assert records[types.index("tool.approval.resolved")]["approved"] is True
    assert records[types.index("tool.result")]["output"] == "changed file.txt"
    assert records[types.index("assistant.message")]["content"] == "done"


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


def test_json_mode_emits_error_event_without_stderr_noise(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module, "build_runtime", build_tool_runtime)

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
    assert [record["type"] for record in records] == ["agent.started", "error"]
    assert records[-1]["message"] == "Maximum tool iterations exceeded: 0"
