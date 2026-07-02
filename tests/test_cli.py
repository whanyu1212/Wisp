from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import anyio
from pytest import MonkeyPatch
from typer.testing import CliRunner

from wisp import cli as cli_module
from wisp.cli import _print_mode_tool_approval_policy, _print_mode_tool_registry, app
from wisp.providers.base import ProviderStreamEvent, ToolCall, ToolCallResult, ToolSpec
from wisp.runtime.api import ExtensionAPI, WispRuntime
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ProviderRegistry, ToolRegistry
from wisp.tools.base import ToolArguments, ToolInputSchema
from wisp.tools.builtin import BashTool, EditTool, FindTool, GrepTool, LsTool, ReadTool, WriteTool
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolResult


class MixedTextToolProvider:
    name = "mixed-tool-test"
    default_model: str | None = "mixed-tool-test"

    async def stream(
        self,
        messages: Sequence[object],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        if not tool_results:
            yield "prefix"
            yield ToolCall(
                call_id="call-1",
                name="danger",
                arguments={"path": "file.txt"},
                response_id="response-1",
            )
            return
        yield "suffix"


class CancellableProvider:
    name = "cancellable-test"
    default_model: str | None = "cancellable-test"

    async def stream(
        self,
        messages: Sequence[object],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        user_prompts = _user_prompts(messages)
        prompt = user_prompts[-1] if user_prompts else ""
        if prompt == "slow":
            yield "working"
            await anyio.sleep_forever()
        if "slow" in user_prompts[:-1]:
            yield "leaked slow"
            return
        yield f"done {prompt}"


class ToolCallingProvider:
    name = "tool-test"
    default_model: str | None = "tool-test"

    async def stream(
        self,
        messages: Sequence[object],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        if not tool_results:
            yield ToolCall(
                call_id="call-1",
                name="danger",
                arguments={"path": "file.txt"},
                response_id="response-1",
            )
            return
        yield "done"


class DangerTool:
    name = "danger"
    safety = "mutating"
    description = "Pretend to mutate a file."
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        return ToolResult(text=f"changed {arguments['path']}")


async def build_tool_runtime() -> WispRuntime:
    providers = ProviderRegistry()
    tools = ToolRegistry()
    events = EventBus()
    api = ExtensionAPI(providers=providers, tools=tools, events=events)
    providers.register(ToolCallingProvider())
    tools.register(DangerTool())
    return WispRuntime(providers=providers, tools=tools, events=events, api=api)


async def build_cancellable_runtime() -> WispRuntime:
    providers = ProviderRegistry()
    tools = ToolRegistry()
    events = EventBus()
    api = ExtensionAPI(providers=providers, tools=tools, events=events)
    providers.register(CancellableProvider())
    return WispRuntime(providers=providers, tools=tools, events=events, api=api)


async def build_mixed_tool_runtime() -> WispRuntime:
    providers = ProviderRegistry()
    tools = ToolRegistry()
    events = EventBus()
    api = ExtensionAPI(providers=providers, tools=tools, events=events)
    providers.register(MixedTextToolProvider())
    tools.register(DangerTool())
    return WispRuntime(providers=providers, tools=tools, events=events, api=api)


def _jsonl_records(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines()]


def _last_user_prompt(messages: Sequence[object]) -> str:
    user_prompts = _user_prompts(messages)
    return user_prompts[-1] if user_prompts else ""


def _user_prompts(messages: Sequence[object]) -> list[str]:
    return [
        str(getattr(message, "content", ""))
        for message in messages
        if getattr(message, "role", None) == "user"
    ]


def test_print_mode_outputs_response_and_writes_session(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-p", "hello", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "fake response to: hello\n"
    assert "session saved:" in result.stderr

    session_files = list(tmp_path.glob("*.jsonl"))
    assert len(session_files) == 1

    records = [
        json.loads(line) for line in session_files[0].read_text(encoding="utf-8").splitlines()
    ]
    assert [record["message"]["role"] for record in records] == [
        "system",
        "system",
        "user",
        "assistant",
    ]
    assert "You are Wisp" in records[0]["message"]["content"]
    assert "allowed tools: none exposed to the model" in records[1]["message"]["content"]


def test_print_mode_continue_appends_to_latest_session(tmp_path: Path) -> None:
    runner = CliRunner()

    first = runner.invoke(
        app,
        ["-p", "first", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )
    second = runner.invoke(
        app,
        ["-p", "second", "--continue", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    session_files = list(tmp_path.glob("*.jsonl"))
    assert len(session_files) == 1
    records = [
        json.loads(line) for line in session_files[0].read_text(encoding="utf-8").splitlines()
    ]
    assert [record["message"]["role"] for record in records] == [
        "system",
        "system",
        "user",
        "assistant",
        "system",
        "system",
        "user",
        "assistant",
    ]
    assert [
        record["message"]["content"] for record in records if record["message"]["role"] == "user"
    ] == [
        "first",
        "second",
    ]


def test_print_mode_resume_appends_to_named_session(tmp_path: Path) -> None:
    runner = CliRunner()

    first = runner.invoke(
        app,
        ["-p", "first", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )
    assert first.exit_code == 0, first.output
    session_file = next(tmp_path.glob("*.jsonl"))

    second = runner.invoke(
        app,
        ["-p", "second", "--resume", session_file.name, "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert second.exit_code == 0, second.output
    records = [json.loads(line) for line in session_file.read_text(encoding="utf-8").splitlines()]
    assert [
        record["message"]["content"] for record in records if record["message"]["role"] == "user"
    ] == [
        "first",
        "second",
    ]


def test_print_mode_rejects_resume_and_continue_together(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-p", "hello", "--resume", "missing", "--continue", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1
    assert "use either --resume or --continue" in result.output


def test_print_mode_reports_missing_resume_session(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-p", "hello", "--resume", "missing", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1
    assert "Session not found: missing" in result.output


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


def test_rpc_mode_runs_prompt_commands_with_explicit_id(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"cmd-1","type":"prompt","prompt":"hello"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "agent.started",
        "token.delta",
        "token.delta",
        "token.delta",
        "token.delta",
        "assistant.message",
        "session.saved",
        "rpc.command.finished",
    ]
    assert records[0]["type"] == "rpc.command.started"
    assert records[0]["command_id"] == "cmd-1"
    assert records[0]["command_type"] == "prompt"
    assert records[-3]["content"] == "fake response to: hello"
    assert records[-1]["command_id"] == "cmd-1"
    assert records[-1]["command_type"] == "prompt"
    assert records[-1]["ok"] is True
    assert records[-1]["error"] is None


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
        record["content"] for record in records if record["type"] == "assistant.message"
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
        "Invalid RPC JSON: Expecting value",
        "RPC command must be a JSON object",
        "Unknown RPC command: missing",
        "RPC prompt command requires string field: prompt",
        "RPC command id must be a non-empty string",
    ]
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [(record["command_id"], record["ok"], record["error"]) for record in finished[:2]] == [
        ("bad", False, "Unknown RPC command: missing"),
        ("missing-prompt", False, "RPC prompt command requires string field: prompt"),
    ]
    assert finished[2]["ok"] is False
    assert finished[2]["error"] == "RPC command id must be a non-empty string"
    assert any(
        record["type"] == "assistant.message" and record["content"] == "fake response to: ok"
        for record in records
    )
    assert finished[-1]["command_id"] == "ok"
    assert finished[-1]["ok"] is True


def test_rpc_mode_rejects_cli_prompt(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-p", "hello", "--mode", "rpc", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1, result.output
    assert result.stderr == ""
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == ["error"]
    assert records[0]["message"] == (
        "--prompt is not used with --mode rpc; send prompt commands on stdin"
    )


def test_rpc_mode_shutdown_emits_lifecycle_and_exits(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"bye","type":"shutdown"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "rpc.command.finished",
    ]
    assert records[0]["command_id"] == "bye"
    assert records[0]["command_type"] == "shutdown"
    assert records[1]["command_id"] == "bye"
    assert records[1]["command_type"] == "shutdown"
    assert records[1]["ok"] is True


def test_rpc_mode_cancels_running_prompt(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module, "build_runtime", build_cancellable_runtime)

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"cmd-1","type":"prompt","prompt":"slow"}\n'
            '{"id":"cancel-1","type":"cancel","target_id":"cmd-1"}\n'
        ),
        env={"WISP_PROVIDER": "cancellable-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    records = _jsonl_records(result.stdout)
    errors = [record["message"] for record in records if record["type"] == "error"]
    assert "RPC command cancelled: cmd-1" in errors
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    cancel_finished = next(record for record in finished if record["command_id"] == "cancel-1")
    prompt_finished = next(record for record in finished if record["command_id"] == "cmd-1")
    assert cancel_finished["ok"] is True
    assert prompt_finished["ok"] is False
    assert prompt_finished["error"] == "RPC command cancelled: cmd-1"


def test_rpc_mode_cancel_reports_unknown_target(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"cancel-1","type":"cancel","target_id":"missing"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "error",
        "rpc.command.finished",
    ]
    assert records[1]["message"] == "No running RPC command with id: missing"
    assert records[2]["command_id"] == "cancel-1"
    assert records[2]["ok"] is False
    assert records[2]["error"] == "No running RPC command with id: missing"


def test_rpc_mode_cancel_requires_target_id(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"cancel-1","type":"cancel"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert records[1]["message"] == "RPC cancel command requires string field: target_id"
    assert records[2]["command_id"] == "cancel-1"
    assert records[2]["ok"] is False


def test_rpc_mode_processes_queued_shutdown_after_running_prompt_finishes(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module, "build_runtime", build_cancellable_runtime)

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"cmd-1","type":"prompt","prompt":"slow"}\n'
            '{"id":"shutdown-1","type":"shutdown"}\n'
            '{"id":"cancel-1","type":"cancel","target_id":"cmd-1"}\n'
        ),
        env={"WISP_PROVIDER": "cancellable-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert "RPC prompt command requires string field: prompt" not in [
        record.get("message") for record in records
    ]
    started = [record for record in records if record["type"] == "rpc.command.started"]
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [record["command_id"] for record in started] == [
        "cmd-1",
        "cancel-1",
        "shutdown-1",
    ]
    assert [(record["command_id"], record["ok"]) for record in finished] == [
        ("cancel-1", True),
        ("cmd-1", False),
        ("shutdown-1", True),
    ]


def test_rpc_mode_excludes_cancelled_prompt_from_next_prompt_history(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module, "build_runtime", build_cancellable_runtime)

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
        record["content"] for record in records if record["type"] == "assistant.message"
    ]
    assert assistant_messages == ["done second"]


def test_rpc_mode_queues_prompts_while_canceling_running_prompt(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module, "build_runtime", build_cancellable_runtime)

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
        record["content"] for record in records if record["type"] == "assistant.message"
    ]
    assert assistant_messages == ["done second"]
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [(record["command_id"], record["ok"]) for record in finished] == [
        ("cancel-1", True),
        ("cmd-1", False),
        ("cmd-2", True),
    ]


def test_print_mode_renders_denied_tool_events_to_stderr(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module, "build_runtime", build_tool_runtime)

    result = runner.invoke(
        app,
        ["-p", "use tool", "--allow-tool", "danger", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "tool-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "done\n"
    assert '→ tool danger {"path": "file.txt"}' in result.stderr
    assert "? approval required for danger (mutating)" in result.stderr
    assert "! denied danger: Tool danger requires approval before execution" in result.stderr
    assert "✗ tool danger: Tool danger requires approval before execution" in result.stderr
    assert "session saved:" in result.stderr
    assert "changed file.txt" not in result.stderr


def test_print_mode_renders_approved_tool_events_to_stderr(
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
            "--allow-tool",
            "danger",
            "--yes",
            "--session-dir",
            str(tmp_path),
        ],
        env={"WISP_PROVIDER": "tool-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "done\n"
    assert '→ tool danger {"path": "file.txt"}' in result.stderr
    assert "? approval required for danger (mutating)" in result.stderr
    assert "✓ approved danger" in result.stderr
    assert "✓ tool danger: changed file.txt" in result.stderr
    assert "session saved:" in result.stderr


def test_print_mode_enforces_explicit_tool_iteration_cap(
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
    assert "error: Maximum tool iterations exceeded: 0" in result.stderr


def test_print_mode_rejects_negative_tool_iteration_cap(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-p", "hello", "--max-tool-iterations", "-1", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1, result.output
    assert "error: --max-tool-iterations must be non-negative" in result.stderr


def test_print_mode_keeps_event_separator_out_of_stdout(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module, "build_runtime", build_mixed_tool_runtime)

    result = runner.invoke(
        app,
        [
            "-p",
            "use tool",
            "--allow-tool",
            "danger",
            "--yes",
            "--session-dir",
            str(tmp_path),
        ],
        env={"WISP_PROVIDER": "mixed-tool-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "prefixsuffix\n"
    assert "\n→ tool danger" in result.stderr
    assert "✓ tool danger: changed file.txt" in result.stderr


def test_print_mode_context_describes_allowed_read_tools(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-p", "hello", "--allow-read-tools", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    session_files = list(tmp_path.glob("*.jsonl"))
    assert len(session_files) == 1
    records = [
        json.loads(line) for line in session_files[0].read_text(encoding="utf-8").splitlines()
    ]
    context = records[1]["message"]["content"]
    assert "allowed tools:" in context
    assert "- read:" in context
    assert "- grep:" in context
    assert "- find:" in context
    assert "- ls:" in context
    assert "- write:" not in context
    assert "- edit:" not in context
    assert "- bash:" not in context


def test_print_mode_requires_approval_for_dangerous_tools_without_yes() -> None:
    approval = _print_mode_tool_approval_policy(False)

    assert approval.approves(ReadTool()) is True
    assert approval.approves(WriteTool()) is False
    assert approval.approves(EditTool()) is False
    assert approval.approves(BashTool()) is False


def test_print_mode_yes_approves_dangerous_tools() -> None:
    approval = _print_mode_tool_approval_policy(True)

    assert approval.approves(WriteTool()) is True
    assert approval.approves(EditTool()) is True
    assert approval.approves(BashTool()) is True


def test_print_mode_exposes_no_tools_by_default() -> None:
    registry = ToolRegistry()
    for tool in (
        ReadTool(),
        WriteTool(),
        EditTool(),
        BashTool(),
        GrepTool(),
        FindTool(),
        LsTool(),
    ):
        registry.register(tool)

    filtered = _print_mode_tool_registry(registry)

    assert filtered.names() == ()


def test_print_mode_can_expose_sandboxed_read_tools() -> None:
    registry = ToolRegistry()
    for tool in (
        ReadTool(),
        WriteTool(),
        EditTool(),
        BashTool(),
        GrepTool(),
        FindTool(),
        LsTool(),
    ):
        registry.register(tool)

    filtered = _print_mode_tool_registry(registry, allow_read_tools=True)

    assert filtered.names() == ("read", "grep", "find", "ls")


def test_print_mode_can_expose_explicit_tools() -> None:
    registry = ToolRegistry()
    for tool in (ReadTool(), WriteTool(), BashTool()):
        registry.register(tool)

    filtered = _print_mode_tool_registry(registry, allowed_tools=("bash", "write"))

    assert filtered.names() == ("write", "bash")


def test_print_mode_reports_unknown_allowed_tool(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-p", "hello", "--allow-tool", "missing", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1
    assert "Unknown tool: missing" in result.output


def test_print_mode_reports_unknown_provider(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-p", "hello", "--provider", "missing", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1
    assert "Unknown provider: missing" in result.output
