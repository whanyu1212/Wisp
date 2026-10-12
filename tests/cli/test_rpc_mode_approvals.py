# ruff: noqa: F403,F405

from __future__ import annotations

import pytest

from tests.support.cli import *
from tests.support.rpc import guard_rpc_command_serialization
from wisp.rpc.commands import ApprovalCommand
from wisp.rpc.handlers.control import handle_rpc_control_command
from wisp.rpc.host import RpcToolApprovalPolicy


def test_rpc_approval_policy_approves_waiting_tool_call(tmp_path: Path) -> None:
    provider = ToolCallingProvider()
    tools = ToolRegistry()
    tools.register(DangerTool())
    approval_policy = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval())

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
    approval_policy = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval())

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
    approval_policy = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval())
    tool = DangerTool()
    approval_policy.prepare_approval(tool, call_id="call-1", arguments={})

    handle_rpc_control_command(
        ApprovalCommand(id="approval-1", call_id="call-1", approved=False, reason="not safe"),
        running_command=None,
        approval_policy=approval_policy,
        write_event=cli_rpc_module._write_json_event,
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
    approval_policy = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval())
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
    approval_policy = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval())
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
    assert RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval()).approves(tool) is False


def test_rpc_approval_policy_remembers_all_unsafe_tools_for_process() -> None:
    approval_policy = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval())
    tool = DangerTool()
    approval_policy.prepare_approval(tool, call_id="call-1", arguments={})

    assert approval_policy.resolve_approval(
        call_id="call-1",
        approved=True,
        scope="all_session",
    )
    assert approval_policy.approves(tool) is True
    assert approval_policy.approves(BashTool()) is True


def test_rpc_mode_denies_pending_approval_when_input_closes(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(rpc_host_module, "build_runtime", build_tool_runtime)

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
        "RPC command does not match the negotiated schema",
        "RPC command does not match the negotiated schema",
        "RPC command does not match the negotiated schema",
    ]


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)
