# ruff: noqa: F403,F405

from __future__ import annotations

import pytest

from tests.cli_support import *
from wisp.agent.transcript import INTERRUPTED_TOOL_RESULT_TEXT
from wisp.events import ToolCallSnapshot


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
        "message.started",
        "message.delta",
        "message.delta",
        "message.delta",
        "message.delta",
        "message.completed",
        "turn.completed",
        "session.saved",
        "agent.completed",
        "rpc.command.finished",
    ]
    assert all(record["schema_version"] == 3 for record in records)
    assert records[0]["type"] == "rpc.command.started"
    assert records[0]["command_id"] == "cmd-1"
    assert records[0]["command_type"] == "prompt"
    assert records[-5]["content"] == "fake response to: hello"
    assert records[-1]["command_id"] == "cmd-1"
    assert records[-1]["command_type"] == "prompt"
    assert records[-1]["ok"] is True
    assert records[-1]["error"] is None


def test_rpc_mode_configures_model_for_future_prompts(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"configure-1","type":"configure","model":"gpt-5.5"}\n'
            '{"id":"cmd-1","type":"prompt","prompt":"hello"}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    configure_finished = records[1]
    assert configure_finished["type"] == "rpc.command.finished"
    assert configure_finished["command_id"] == "configure-1"
    assert configure_finished["command_type"] == "configure"
    assert configure_finished["ok"] is True
    assert configure_finished["error"] is None
    assert any(record.get("content") == "fake response to: hello" for record in records)


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
        record["type"] == "message.completed" and record["content"] == "fake response to: ok"
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
    monkeypatch.setattr(cli_module.rpc, "build_runtime", build_cancellable_runtime)

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


class _TrustedGate:
    async def resolve(self) -> bool:
        return True


def test_rpc_prompt_cancellation_rolls_back_before_completion_boundary(tmp_path: Path) -> None:
    class PreResponseCancellableProvider(CancellableProvider):
        def __init__(self, started: anyio.Event) -> None:
            self.started = started

        async def stream(
            self,
            messages: Sequence[Message],
            *,
            model: str | None = None,
            tools: Sequence[ToolSpec] = (),
            tool_results: Sequence[ToolCallResult] = (),
            previous_response_id: str | None = None,
        ) -> AsyncIterator[ProviderEvent]:
            self.started.set()
            await anyio.sleep_forever()
            async for event in super().stream(
                messages,
                model=model,
                tools=tools,
                tool_results=tool_results,
                previous_response_id=previous_response_id,
            ):
                yield event

    async def run_prompt() -> object:
        started = anyio.Event()
        session = JsonlSessionStore(tmp_path).create()
        agent = CodingSession(
            provider=PreResponseCancellableProvider(started),
            sessions=JsonlSessionStore(tmp_path),
        )
        cancel_scope = anyio.CancelScope()
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(
                    cli_module.rpc._run_rpc_prompt_command,
                    agent,
                    session,
                    (),
                    0,
                    "slow",
                    "cmd-1",
                    "prompt",
                    cancel_scope,
                    send.clone(),
                    _TrustedGate(),
                )
                await started.wait()
                cancel_scope.cancel()
                completed = await receive.receive()

        assert not session.path.exists()
        return completed

    completed = anyio.run(run_prompt)

    assert completed.ok is False
    assert completed.command_id == "cmd-1"
    assert completed.history is not None
    assert completed.history == ()
    assert completed.entry_count == 0


def test_rpc_prestart_cancellation_preserves_loaded_tool_repair(tmp_path: Path) -> None:
    class PreResponseCancellableProvider(CancellableProvider):
        def __init__(self, started: anyio.Event) -> None:
            self.started = started

        async def stream(
            self,
            messages: Sequence[Message],
            *,
            model: str | None = None,
            tools: Sequence[ToolSpec] = (),
            tool_results: Sequence[ToolCallResult] = (),
            previous_response_id: str | None = None,
        ) -> AsyncIterator[ProviderEvent]:
            self.started.set()
            await anyio.sleep_forever()
            async for event in super().stream(
                messages,
                model=model,
                tools=tools,
                tool_results=tool_results,
                previous_response_id=previous_response_id,
            ):
                yield event

    async def run_prompt() -> object:
        started = anyio.Event()
        session = JsonlSessionStore(tmp_path).create()
        await session.append_message(Message(role="user", content="old prompt"))
        await session.append_message(
            Message(
                role="assistant",
                content="",
                tool_calls=(
                    ToolCallSnapshot(
                        call_id="call-1",
                        name="read",
                        arguments={"path": "README.md"},
                    ),
                ),
                finish_reason="tool_calls",
            )
        )
        committed_history = session.read_messages()
        entry_start = len(session.read_entries())
        agent = CodingSession(
            provider=PreResponseCancellableProvider(started),
            sessions=JsonlSessionStore(tmp_path),
        )
        cancel_scope = anyio.CancelScope()
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(
                    cli_module.rpc._run_rpc_prompt_command,
                    agent,
                    session,
                    committed_history,
                    entry_start,
                    "cancelled prompt",
                    "cmd-1",
                    "prompt",
                    cancel_scope,
                    send.clone(),
                    _TrustedGate(),
                )
                await started.wait()
                cancel_scope.cancel()
                completed = await receive.receive()

        messages = session.read_messages()
        assert [message.role for message in messages] == ["user", "assistant", "tool"]
        assert messages[-1].content == INTERRUPTED_TOOL_RESULT_TEXT
        return completed

    completed = anyio.run(run_prompt)

    assert completed.ok is False
    assert completed.history is not None
    assert [message.role for message in completed.history] == ["user", "assistant", "tool"]
    assert completed.entry_count == 3


def test_rpc_prompt_cancellation_retains_entries_after_completion_boundary(
    tmp_path: Path,
) -> None:
    class BlockingDangerTool:
        name = "danger"
        safety = "read"
        description = "Block after the provider completes its tool-call turn."
        input_schema: ToolInputSchema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }

        def __init__(self, started: anyio.Event) -> None:
            self.started = started

        async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
            self.started.set()
            await anyio.sleep_forever()
            return ToolResult(text="unused")

    async def run_prompt() -> object:
        started = anyio.Event()
        session = JsonlSessionStore(tmp_path).create()
        tools = ToolRegistry()
        tools.register(BlockingDangerTool(started))
        agent = CodingSession(
            provider=ToolCallingProvider(),
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
        )
        cancel_scope = anyio.CancelScope()
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(
                    cli_module.rpc._run_rpc_prompt_command,
                    agent,
                    session,
                    (),
                    0,
                    "use tool",
                    "cmd-1",
                    "prompt",
                    cancel_scope,
                    send.clone(),
                    _TrustedGate(),
                )
                await started.wait()
                cancel_scope.cancel()
                completed = await receive.receive()

        retained = [
            message
            for message in session.read_messages()
            if message.role in {"user", "assistant", "tool"}
        ]
        assert [message.role for message in retained] == ["user", "assistant", "tool"]
        assert retained[0].content == "use tool"
        assert retained[1].tool_calls is not None
        assert [call.call_id for call in retained[1].tool_calls] == ["call-1"]
        assert retained[2].tool_call_id == "call-1"
        assert retained[2].content == INTERRUPTED_TOOL_RESULT_TEXT
        assert retained[2].is_error is True
        return completed

    completed = anyio.run(run_prompt)

    assert completed.ok is False
    assert completed.command_id == "cmd-1"
    assert completed.history is not None
    assert [
        message.role
        for message in completed.history
        if message.role in {"user", "assistant", "tool"}
    ] == ["user", "assistant", "tool"]
    assert completed.entry_count > 0


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


def test_rpc_approval_policy_approves_waiting_tool_call(tmp_path: Path) -> None:
    provider = ToolCallingProvider()
    tools = ToolRegistry()
    tools.register(DangerTool())
    approval_policy = cli_module._RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval())

    async def run_agent() -> list[object]:
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
            tool_approval_policy=approval_policy,
        )
        events: list[object] = []
        async for event in agent.run("use tool"):
            events.append(event)
            if isinstance(event, ToolApprovalRequested):
                assert approval_policy.resolve_approval(call_id=event.call_id, approved=True)
        return events

    events = anyio.run(run_agent)

    resolved = next(event for event in events if isinstance(event, ToolApprovalResolved))
    result = next(event for event in events if isinstance(event, ToolResultReady))
    assert resolved.approved is True
    assert resolved.reason is None
    assert result.output == "changed file.txt"
    assert result.is_error is False


def test_rpc_approval_policy_denies_waiting_tool_call(tmp_path: Path) -> None:
    provider = ToolCallingProvider()
    tools = ToolRegistry()
    tools.register(DangerTool())
    approval_policy = cli_module._RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval())

    async def run_agent() -> list[object]:
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
            tool_approval_policy=approval_policy,
        )
        events: list[object] = []
        async for event in agent.run("use tool"):
            events.append(event)
            if isinstance(event, ToolApprovalRequested):
                assert approval_policy.resolve_approval(
                    call_id=event.call_id,
                    approved=False,
                    reason="not allowed",
                )
        return events

    events = anyio.run(run_agent)

    resolved = next(event for event in events if isinstance(event, ToolApprovalResolved))
    result = next(event for event in events if isinstance(event, ToolResultReady))
    assert resolved.approved is False
    assert resolved.reason == "not allowed"
    assert result.output == "not allowed"
    assert result.is_error is True


def test_rpc_approval_command_resolves_pending_approval(
    monkeypatch: MonkeyPatch,
) -> None:
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    approval_policy = cli_module._RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval())
    tool = DangerTool()
    approval_policy.prepare_approval(tool, call_id="call-1", arguments={})

    cli_module._handle_rpc_control_command(
        {
            "id": "approval-1",
            "type": "approval",
            "call_id": "call-1",
            "approved": False,
            "reason": "not safe",
        },
        running_prompt=None,
        approval_policy=approval_policy,
    )

    async def wait_for_decision() -> object:
        return await approval_policy.await_approval(tool, call_id="call-1", arguments={})

    decision = anyio.run(wait_for_decision)
    records = _jsonl_records(output.getvalue())
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "rpc.command.finished",
    ]
    assert records[1]["command_id"] == "approval-1"
    assert records[1]["ok"] is True
    assert decision == cli_module.ToolApprovalDecision(approved=False, reason="not safe")


def test_rpc_approval_policy_rejects_duplicate_decisions() -> None:
    approval_policy = cli_module._RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval())
    tool = DangerTool()
    approval_policy.prepare_approval(tool, call_id="call-1", arguments={})

    assert approval_policy.resolve_approval(
        call_id="call-1",
        approved=False,
        reason="first decision",
    )
    assert not approval_policy.resolve_approval(call_id="call-1", approved=True)

    async def wait_for_decision() -> object:
        return await approval_policy.await_approval(tool, call_id="call-1", arguments={})

    decision = anyio.run(wait_for_decision)
    assert decision == cli_module.ToolApprovalDecision(
        approved=False,
        reason="first decision",
    )


def test_rpc_approval_policy_remembers_exact_tool_for_process() -> None:
    approval_policy = cli_module._RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval())
    tool = DangerTool()
    other_tool = BashTool()
    approval_policy.prepare_approval(tool, call_id="call-1", arguments={})

    assert approval_policy.resolve_approval(
        call_id="call-1",
        approved=True,
        scope="tool_session",
    )
    assert approval_policy.approves(tool) is True
    assert approval_policy.requires_approval(other_tool) is True
    assert (
        cli_module._RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval()).approves(tool)
        is False
    )


def test_rpc_approval_policy_remembers_all_unsafe_tools_for_process() -> None:
    approval_policy = cli_module._RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval())
    tool = DangerTool()
    approval_policy.prepare_approval(tool, call_id="call-1", arguments={})

    assert approval_policy.resolve_approval(
        call_id="call-1",
        approved=True,
        scope="all_session",
    )
    assert approval_policy.approves(tool) is True
    assert approval_policy.approves(BashTool()) is True


@pytest.mark.parametrize(
    ("scope", "approved", "message"),
    [
        ("forever", True, "field scope must be one of"),
        ([], True, "field scope must be one of"),
        ("tool_session", False, "scope is only valid for approved requests"),
    ],
)
def test_rpc_approval_command_rejects_invalid_scope(
    monkeypatch: MonkeyPatch,
    scope: object,
    approved: bool,
    message: str,
) -> None:
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    approval_policy = cli_module._RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval())
    approval_policy.prepare_approval(DangerTool(), call_id="call-1", arguments={})

    cli_module._handle_rpc_control_command(
        {
            "id": "approval-1",
            "type": "approval",
            "call_id": "call-1",
            "approved": approved,
            "scope": scope,
        },
        running_prompt=None,
        approval_policy=approval_policy,
    )

    records = _jsonl_records(output.getvalue())
    assert records[-1]["ok"] is False
    assert message in str(records[-1]["error"])


def test_rpc_mode_denies_pending_approval_when_input_closes(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module.rpc, "build_runtime", build_tool_runtime)

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--allow-tool", "danger", "--session-dir", str(tmp_path)],
        input='{"id":"cmd-1","type":"prompt","prompt":"use tool"}\n',
        env={"WISP_PROVIDER": "tool-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    approval_resolved = next(
        record for record in records if record["type"] == "tool.approval.resolved"
    )
    assert approval_resolved["approved"] is False
    assert approval_resolved["reason"] == "RPC input closed before approval response"
    tool_result = next(record for record in records if record["type"] == "tool.result")
    assert tool_result["is_error"] is True
    assert tool_result["output"] == "RPC input closed before approval response"
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [(record["command_id"], record["ok"]) for record in finished] == [("cmd-1", True)]


def test_rpc_mode_approval_reports_unknown_call_id(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"approval-1","type":"approval","call_id":"missing","approved":true}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "error",
        "rpc.command.finished",
    ]
    assert records[1]["message"] == "No pending tool approval with call_id: missing"
    assert records[2]["command_id"] == "approval-1"
    assert records[2]["ok"] is False


def test_rpc_mode_approval_requires_valid_fields(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"approval-1","type":"approval","approved":true}\n'
            '{"id":"approval-2","type":"approval","call_id":"call-1"}\n'
            '{"id":"approval-3","type":"approval","call_id":"call-1","approved":false,"reason":3}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    errors = [record["message"] for record in records if record["type"] == "error"]
    assert errors == [
        "RPC approval command requires string field: call_id",
        "RPC approval command requires boolean field: approved",
        "RPC approval command field reason must be a string",
    ]


def test_rpc_mode_rejects_commands_beyond_queue_cap_while_prompt_runs(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module.rpc, "build_runtime", build_cancellable_runtime)
    monkeypatch.setattr(cli_module.rpc, "_MAX_QUEUED_RPC_COMMANDS", 2)

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"cmd-1","type":"prompt","prompt":"slow"}\n'
            '{"id":"cmd-2","type":"prompt","prompt":"second"}\n'
            '{"id":"cmd-3","type":"prompt","prompt":"third"}\n'
            '{"id":"cmd-overflow","type":"prompt","prompt":"overflow"}\n'
            '{"id":"cancel-1","type":"cancel","target_id":"cmd-1"}\n'
        ),
        env={"WISP_PROVIDER": "cancellable-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    overflow_error = next(
        record
        for record in records
        if record["type"] == "rpc.command.finished" and record["command_id"] == "cmd-overflow"
    )
    assert overflow_error["ok"] is False
    assert overflow_error["error"] == "RPC command queue is full while a prompt is running"
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert ("cancel-1", True) in [(record["command_id"], record["ok"]) for record in finished]
    assistant_messages = [
        record["content"] for record in records if record["type"] == "message.completed"
    ]
    assert assistant_messages == ["done second", "done third"]


def test_rpc_mode_processes_queued_shutdown_after_running_prompt_finishes(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module.rpc, "build_runtime", build_cancellable_runtime)

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


def test_rpc_mode_finishes_command_when_session_write_fails_before_file_exists(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()

    async def fail_append_message(
        self: JsonlSession,
        message: Message,
    ) -> object:
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
    monkeypatch.setattr(cli_module.rpc, "build_runtime", build_failing_runtime)

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
    assistant_messages = [
        record["content"] for record in records if record["type"] == "message.completed"
    ]
    assert assistant_messages == ["saw failed history"]
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
    monkeypatch.setattr(cli_module.rpc, "build_runtime", build_cancellable_runtime)

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


def test_rpc_mode_queues_prompts_while_canceling_running_prompt(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module.rpc, "build_runtime", build_cancellable_runtime)

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
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [(record["command_id"], record["ok"]) for record in finished] == [
        ("cancel-1", True),
        ("cmd-1", False),
        ("cmd-2", True),
    ]
