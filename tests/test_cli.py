from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

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


async def build_mixed_tool_runtime() -> WispRuntime:
    providers = ProviderRegistry()
    tools = ToolRegistry()
    events = EventBus()
    api = ExtensionAPI(providers=providers, tools=tools, events=events)
    providers.register(MixedTextToolProvider())
    tools.register(DangerTool())
    return WispRuntime(providers=providers, tools=tools, events=events, api=api)


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
