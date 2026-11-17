"""Scenario assertions against the real RPC host and explicit broken streams."""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Literal

import anyio
import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

import wisp.sdk as sdk_module
from evals.tool_use.scenario import (
    ApprovalDecision,
    CheckpointStep,
    CompactStep,
    NavigateStep,
    PromptStep,
    ResumeStep,
    Scenario,
)
from evals.tool_use.scenario_driver import ScenarioConfig, execute_scenario
from tests.cli.rpc_mode_support import VALID_COMPACTION_SUMMARY, runtime_with_provider
from wisp.config.runtime import WispConfig
from wisp.events import (
    AgentCompleted,
    CompactionCompleted,
    KnownWispEvent,
    RpcCommandFinished,
    RpcSessionTreeReported,
    RpcWorkspaceReported,
    ToolApprovalRequested,
    ToolApprovalResolved,
    WispEvent,
)
from wisp.messages import Message
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderToolCallCompleted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider
from wisp.rpc.client import JsonlSubprocessRpcTransport, RpcTransport
from wisp.rpc.commands import RpcCommand
from wisp.runtime.api import WispRuntime
from wisp.sdk import InProcessOptions, InProcessWisp
from wisp.sessions.jsonl import JsonlSession
from wisp.sessions.replay import HISTORICAL_CONTEXT_SUMMARY_LABEL
from wisp.tools.base import ToolArguments
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolResult


class _RecordTool:
    name = "record"
    description = "Record a deterministic test side effect."
    safety: Literal["mutating"] = "mutating"
    input_schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }

    def __init__(self) -> None:
        self.values: list[object] = []

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        self.values.append(arguments["value"])
        return ToolResult(text="recorded")


def _host_factory(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    provider: ScriptedProvider,
    tool: _RecordTool,
    *,
    max_tool_iterations: int | None = None,
) -> Callable[[], Awaitable[RpcTransport]]:
    async def build(config: WispConfig) -> WispRuntime:
        runtime = await runtime_with_provider(provider)
        runtime.tools.register(tool)
        return runtime

    monkeypatch.setattr(sdk_module, "build_session_runtime", build)

    async def start() -> RpcTransport:
        controller = await InProcessWisp.start(
            WispConfig(
                provider="scripted",
                session_dir=tmp_path / "sessions",
                auto_compaction_enabled=False,
            ),
            options=InProcessOptions(
                startup_trusted=True,
                cwd=tmp_path,
                project_context_root=tmp_path,
                all_tools=True,
                max_tool_iterations=max_tool_iterations,
            ),
        )
        return controller._in_process_transport

    return start


def _answer(text: str) -> tuple[ProviderResponseStarted | ProviderResponseCompleted, ...]:
    return (ProviderResponseStarted(model="scripted"), ProviderResponseCompleted(content=text))


def _call(
    call_id: str, value: str
) -> tuple[ProviderResponseStarted | ProviderToolCallCompleted | ProviderResponseCompleted, ...]:
    call = ToolCall(call_id=call_id, name="record", arguments={"value": value})
    return (
        ProviderResponseStarted(model="scripted"),
        ProviderToolCallCompleted(tool_call=call),
        ProviderResponseCompleted(content="", tool_calls=(call,), finish_reason="tool_calls"),
    )


@pytest.mark.parametrize(
    "steps",
    [
        [],
        [{"type": "resume"}],
        [{"type": "prompt", "prompt": "x", "unknown": True}],
        [{"type": "prompt", "prompt": "x"}, {"type": "navigate", "checkpoint": "later"}],
        [
            {"type": "prompt", "prompt": "x"},
            {"type": "checkpoint", "name": "same"},
            {"type": "checkpoint", "name": "same"},
        ],
    ],
)
def test_rejects_invalid_steps(steps: list[dict[str, object]]) -> None:
    with pytest.raises(ValidationError):
        Scenario.model_validate({"steps": steps})


def test_approval_scope_defaults() -> None:
    assert ApprovalDecision(name="record", arguments={}, approved=True).scope == "once"
    assert ApprovalDecision(name="record", arguments={}, approved=False).scope is None
    with pytest.raises(ValidationError, match="denied approvals"):
        ApprovalDecision(name="record", arguments={}, approved=False, scope="once")


def test_branches_survive_fresh_backend(tmp_path: Path) -> None:
    """Use a real subprocess and handshake, including a restart on the same store."""
    events: list[WispEvent] = []
    backends: list[JsonlSubprocessRpcTransport] = []

    async def start() -> RpcTransport:
        transport = await JsonlSubprocessRpcTransport.start(
            (
                sys.executable,
                "-m",
                "wisp",
                "--mode",
                "rpc",
                "--provider",
                "fake",
                "--session-dir",
                str(tmp_path / "sessions"),
            ),
            cwd=tmp_path,
            env={**os.environ, "WISP_TRUST": "1"},
        )
        backends.append(transport)
        return transport

    scenario = Scenario(
        steps=(
            PromptStep(prompt="first", expect_text="first"),
            CheckpointStep(name="first"),
            PromptStep(prompt="second", expect_text="second"),
            CheckpointStep(name="second"),
            NavigateStep(checkpoint="first"),
            PromptStep(prompt="branch", expect_text="branch"),
            ResumeStep(),
            NavigateStep(checkpoint="second"),
            PromptStep(prompt="last", expect_text="last"),
        )
    )
    result = anyio.run(execute_scenario, scenario, start, events.append)
    assert result.error is None
    assert result.failed_checks == ()
    assert result.completed_steps == 9
    assert len(backends) == 2
    terminals = [event for event in events if isinstance(event, AgentCompleted)]
    assert len(terminals) == 4
    assert len({event.session_id for event in terminals}) == 1
    reports = [event for event in events if isinstance(event, RpcSessionTreeReported)]
    assert reports[0].active_leaf_id != reports[1].active_leaf_id
    assert reports[3].active_leaf_id == reports[0].active_leaf_id
    assert reports[4].active_leaf_id == reports[5].active_leaf_id
    assert reports[7].active_leaf_id == reports[1].active_leaf_id
    assert all(backend._process.returncode is not None for backend in backends)


@pytest.mark.parametrize("approved", [True, False])
def test_once_or_denied_approval(tmp_path: Path, monkeypatch: MonkeyPatch, approved: bool) -> None:
    tool = _RecordTool()
    provider = ScriptedProvider([_call("call-1", "first"), _answer("finished")])
    events: list[WispEvent] = []
    scenario = Scenario(
        steps=(
            PromptStep(
                prompt="record",
                approvals=(
                    ApprovalDecision(
                        name="record", arguments={"value": "first"}, approved=approved
                    ),
                ),
            ),
        )
    )
    result = anyio.run(
        execute_scenario,
        scenario,
        _host_factory(tmp_path, monkeypatch, provider, tool),
        events.append,
    )
    assert result.error is None
    assert result.failed_checks == ()
    assert tool.values == (["first"] if approved else [])
    resolved = [event for event in events if isinstance(event, ToolApprovalResolved)]
    assert len(resolved) == 1 and resolved[0].approved is approved


@pytest.mark.parametrize("scope", ["once", "tool_session"])
def test_session_grant_avoids_second_request(
    tmp_path: Path, monkeypatch: MonkeyPatch, scope: Literal["once", "tool_session"]
) -> None:
    tool = _RecordTool()
    provider = ScriptedProvider(
        [_call("one", "first"), _answer("one"), _call("two", "second"), _answer("two")]
    )
    second = (
        (ApprovalDecision(name="record", arguments={"value": "second"}, approved=True),)
        if scope == "once"
        else ()
    )
    scenario = Scenario(
        steps=(
            PromptStep(
                prompt="first",
                approvals=(
                    ApprovalDecision(
                        name="record", arguments={"value": "first"}, approved=True, scope=scope
                    ),
                ),
            ),
            PromptStep(prompt="second", approvals=second),
        )
    )
    events: list[WispEvent] = []
    result = anyio.run(
        execute_scenario,
        scenario,
        _host_factory(tmp_path, monkeypatch, provider, tool),
        events.append,
    )
    assert result.error is None
    assert result.failed_checks == ()
    assert result.completed_steps == 2
    assert tool.values == ["first", "second"]
    requests = [event for event in events if isinstance(event, ToolApprovalRequested)]
    assert len(requests) == (2 if scope == "once" else 1)


@pytest.mark.parametrize("expectation", ["unexpected", "mismatch", "missing", "extra"])
def test_approval_expectations_fail(
    tmp_path: Path, monkeypatch: MonkeyPatch, expectation: str
) -> None:
    tool = _RecordTool()
    decision = ApprovalDecision(name="record", arguments={"value": "first"}, approved=True)
    approvals = () if expectation == "unexpected" else (decision,)
    if expectation == "mismatch":
        approvals = (decision.model_copy(update={"arguments": {"value": "wrong"}}),)
    elif expectation == "extra":
        approvals = (decision, decision)
    streams = (
        [_answer("finished")]
        if expectation == "missing"
        else [_call("one", "first"), _answer("finished")]
    )
    scenario = Scenario(steps=(PromptStep(prompt="record", approvals=approvals),))
    result = anyio.run(
        execute_scenario,
        scenario,
        _host_factory(tmp_path, monkeypatch, ScriptedProvider(streams), tool),
        lambda event: None,
    )
    assert result.error is None
    assert result.failed_checks
    assert tool.values == (["first"] if expectation == "extra" else [])


def test_compaction_emits_completion(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    provider = ScriptedProvider(
        [_answer("one"), _answer("two"), _answer(VALID_COMPACTION_SUMMARY), _answer("after")]
    )
    events: list[WispEvent] = []
    scenario = Scenario(
        steps=(
            PromptStep(prompt="one"),
            PromptStep(prompt="two"),
            CompactStep(instructions="Preserve the goal"),
            ResumeStep(),
            PromptStep(prompt="after", expect_text="after"),
        )
    )
    result = anyio.run(
        execute_scenario,
        scenario,
        _host_factory(tmp_path, monkeypatch, provider, _RecordTool()),
        events.append,
    )
    assert result.error is None
    assert result.failed_checks == ()
    assert result.completed_steps == 5
    compaction = next(event for event in events if isinstance(event, CompactionCompleted))
    assert compaction.outcome == "completed"
    assert compaction.replaced_entry_count > 0
    resumed_history = provider.calls[-1].messages
    assert any(
        message.content.startswith(HISTORICAL_CONTEXT_SUMMARY_LABEL)
        and VALID_COMPACTION_SUMMARY in message.content
        for message in resumed_history
    )
    assert any(message.role == "user" and message.content == "two" for message in resumed_history)
    assert not any(
        message.role == "user" and message.content == "one" for message in resumed_history
    )


def test_failed_prompt_stops_scenario(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    provider = ScriptedProvider(
        [
            (
                ProviderResponseStarted(model="scripted"),
                ProviderResponseFailed(message="provider failed"),
            ),
            _answer("ok"),
        ]
    )
    scenario = Scenario(steps=(PromptStep(prompt="bad"), PromptStep(prompt="good")))
    result = anyio.run(
        execute_scenario,
        scenario,
        _host_factory(tmp_path, monkeypatch, provider, _RecordTool()),
        lambda event: None,
    )
    assert result.error is None
    assert result.failed_checks
    assert len(provider.calls) == 1
    assert result.completed_steps == 1


class _BrokenTransport:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.commands: list[RpcCommand] = []
        self.closed = False

    async def send(self, command: RpcCommand) -> None:
        self.commands.append(command)

    async def close(self) -> None:
        self.closed = True

    async def events(self) -> AsyncIterator[KnownWispEvent]:
        if self.mode == "blocked":
            await anyio.sleep_forever()
        if self.mode == "eof":
            return
        index = 0
        while index < len(self.commands):
            command = self.commands[index]
            index += 1
            if command.type == "prompt" and self.mode == "boolean-approval":
                yield ToolApprovalRequested(
                    call_id="call",
                    name="mcp_call",
                    target="record",
                    arguments={"value": True},
                    safety="mutating",
                )
            if command.type == "prompt" and self.mode != "missing-agent":
                yield AgentCompleted(session_id="session", turns=1, outcome="completed")
            yield RpcCommandFinished(command_id=command.id, command_type=command.type, ok=True)


@pytest.mark.parametrize(
    ("mode", "error", "timed_out", "failed"),
    [
        ("eof", "RPC stream ended", False, False),
        ("blocked", None, True, True),
        ("missing-agent", None, False, True),
        ("missing-compaction", None, False, True),
    ],
)
def test_broken_streams_close(mode: str, error: str | None, timed_out: bool, failed: bool) -> None:
    transport = _BrokenTransport(mode)

    async def start() -> RpcTransport:
        return transport

    async def run():
        return await execute_scenario(
            Scenario(steps=(PromptStep(prompt="test"), CompactStep())),
            start,
            lambda event: None,
            timeout_seconds=0.05,
        )

    result = anyio.run(run)
    assert transport.closed
    assert result.timed_out is timed_out
    assert bool(result.failed_checks) is failed
    if error is not None:
        assert error in (result.error or "")
    else:
        assert result.error is None


def test_cancellation_propagates_cleanup() -> None:
    transport = _BrokenTransport("blocked")

    async def start() -> RpcTransport:
        return transport

    async def run() -> None:
        with anyio.move_on_after(0.05) as scope:
            await execute_scenario(
                Scenario(steps=(PromptStep(prompt="test"),)), start, lambda event: None
            )
            pytest.fail("external cancellation was swallowed")
        assert scope.cancel_called

    anyio.run(run)
    assert transport.closed


@pytest.mark.parametrize("backend_available", [True, False])
def test_driver_writes_result(tmp_path: Path, backend_available: bool) -> None:
    result_path = tmp_path / "result.json"
    config = ScenarioConfig(
        scenario=Scenario(steps=(PromptStep(prompt="hello", expect_text="absent"),)),
        command=(
            (sys.executable, "-m", "wisp", "--mode", "rpc", "--provider", "fake")
            if backend_available
            else (str(tmp_path / "missing-backend"),)
        ),
        cwd=tmp_path,
        timeout_seconds=20,
        result_path=result_path,
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")

    async def capture() -> anyio.CompletedProcess:
        return await anyio.run_process(
            (sys.executable, "-m", "evals.tool_use.scenario_driver", str(config_path)),
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "WISP_TRUST": "1"},
            check=False,
        )

    process = anyio.run(capture)
    assert process.returncode == (0 if backend_available else 1)
    from evals.tool_use.scenario import ScenarioResult

    result = ScenarioResult.model_validate_json(result_path.read_text(encoding="utf-8"))
    if backend_available:
        assert result.error is None
        assert result.failed_checks == ("prompt response missing text 'absent'",)
        assert b'"type":"agent.completed"' in process.stdout
    else:
        assert "FileNotFoundError" in (result.error or "")
        assert result.failed_checks == ()


def test_exact_approval_argument_types() -> None:
    transport = _BrokenTransport("boolean-approval")

    async def start() -> RpcTransport:
        return transport

    scenario = Scenario(
        steps=(
            PromptStep(
                prompt="record",
                approvals=(
                    ApprovalDecision(
                        name="record",
                        arguments={"value": 1},
                        approved=True,
                    ),
                ),
            ),
        )
    )
    result = anyio.run(execute_scenario, scenario, start, lambda event: None)
    assert result.error is None
    assert result.failed_checks
    assert transport.closed
    approval = transport.commands[-1]
    assert approval.type == "approval"
    assert approval.approved is False


@pytest.mark.parametrize("failure", ["provider", "network", "auth"])
def test_compaction_failure_is_infrastructure(
    tmp_path: Path, monkeypatch: MonkeyPatch, failure: str
) -> None:
    errors = {
        "provider": ProviderResponseFailed(message="provider unavailable"),
        "network": OSError("connection reset"),
        "auth": RuntimeError("invalid API key"),
    }
    provider = ScriptedProvider(
        [
            _answer("one"),
            _answer("two"),
            (ProviderResponseStarted(model="scripted"), errors[failure]),
        ]
    )
    events: list[WispEvent] = []
    result = anyio.run(
        execute_scenario,
        Scenario(steps=(PromptStep(prompt="one"), PromptStep(prompt="two"), CompactStep())),
        _host_factory(tmp_path, monkeypatch, provider, _RecordTool()),
        events.append,
    )
    assert result.error is not None
    assert "compact command failed: Compaction summary failed:" in result.error
    assert result.completed_steps == 2
    assert result.failed_checks == ()
    assert len([event for event in events if isinstance(event, AgentCompleted)]) == 2


@pytest.mark.parametrize(
    "summary", ["", "invalid summary", "## Goal\n## Constraints & Preferences"]
)
def test_invalid_summary_is_task_failure(
    tmp_path: Path, monkeypatch: MonkeyPatch, summary: str
) -> None:
    provider = ScriptedProvider([_answer("one"), _answer("two"), _answer(summary)])
    result = anyio.run(
        execute_scenario,
        Scenario(steps=(PromptStep(prompt="one"), PromptStep(prompt="two"), CompactStep())),
        _host_factory(tmp_path, monkeypatch, provider, _RecordTool()),
        lambda event: None,
    )
    assert result.error is None
    assert result.failed_checks
    assert result.completed_steps == 3


def test_session_failure_is_infrastructure(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    def unreadable_tree(*args: object, **kwargs: object) -> object:
        raise OSError("session tree read failed")

    monkeypatch.setattr(JsonlSession, "read_tree_page", unreadable_tree)
    provider = ScriptedProvider([_answer("one")])
    events: list[WispEvent] = []
    result = anyio.run(
        execute_scenario,
        Scenario(steps=(PromptStep(prompt="one"), CheckpointStep(name="one"))),
        _host_factory(tmp_path, monkeypatch, provider, _RecordTool()),
        events.append,
    )
    assert result.error is not None
    assert "get_session_tree command failed: session tree read failed" in result.error
    assert result.completed_steps == 1
    assert result.failed_checks == ()
    assert any(isinstance(event, AgentCompleted) for event in events)


@pytest.mark.parametrize("expected_value", [True, 1])
def test_failed_approval_acknowledgement(expected_value: bool | int) -> None:
    class FailedApprovalTransport(_BrokenTransport):
        async def events(self) -> AsyncIterator[KnownWispEvent]:
            async for event in super().events():
                if isinstance(event, RpcCommandFinished) and event.command_type == "approval":
                    yield event.model_copy(update={"ok": False, "error": "approval backend failed"})
                else:
                    yield event

    transport = FailedApprovalTransport("boolean-approval")

    async def start() -> RpcTransport:
        return transport

    result = anyio.run(
        execute_scenario,
        Scenario(
            steps=(
                PromptStep(
                    prompt="test",
                    approvals=(
                        ApprovalDecision(
                            name="record",
                            arguments={"value": expected_value},
                            approved=True,
                        ),
                    ),
                ),
            )
        ),
        start,
        lambda event: None,
    )
    assert transport.closed
    if expected_value is True:
        assert "approval command failed: approval backend failed" in (result.error or "")
        assert result.failed_checks == ()
    else:
        assert result.error is None
        assert any("unexpected" in check for check in result.failed_checks)


@pytest.mark.parametrize("timing", ["before_run", "after_completion"])
def test_prompt_backend_failure_is_infrastructure(
    tmp_path: Path, monkeypatch: MonkeyPatch, timing: str
) -> None:
    from wisp.rpc.session import run as session_run

    calls = 0
    if timing == "after_completion":
        original_refresh = session_run.updated_rpc_session_state

        def fail_second_refresh(
            session: JsonlSession, committed_history: tuple[Message, ...], entry_start: int
        ) -> tuple[int, tuple[Message, ...]]:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("prompt session refresh failed")
            return original_refresh(session, committed_history, entry_start)

        monkeypatch.setattr(session_run, "updated_rpc_session_state", fail_second_refresh)
    else:
        original_workspace = session_run.rpc_workspace_report

        async def fail_second_workspace(cwd: Path) -> RpcWorkspaceReported:
            nonlocal calls
            calls += 1
            # Each prompt publishes the workspace before its run and after refresh.
            if calls == 3:
                raise OSError("prompt workspace read failed")
            return await original_workspace(cwd)

        monkeypatch.setattr(session_run, "rpc_workspace_report", fail_second_workspace)
    provider = ScriptedProvider([_answer("one"), _answer("two"), _answer("unreached")])
    events: list[WispEvent] = []
    result = anyio.run(
        execute_scenario,
        Scenario(
            steps=(PromptStep(prompt="one"), PromptStep(prompt="two"), PromptStep(prompt="three"))
        ),
        _host_factory(tmp_path, monkeypatch, provider, _RecordTool()),
        events.append,
    )
    assert "prompt command failed: prompt " in (result.error or "")
    assert result.failed_checks == ()
    assert result.completed_steps == 1
    completions = [event for event in events if isinstance(event, AgentCompleted)]
    assert len(completions) == (2 if timing == "after_completion" else 1)
    assert all(event.outcome == "completed" for event in completions)
    assert len(provider.calls) == len(completions)


def test_prompt_iteration_limit_is_task_failure(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    from evals.tool_use.metrics import measure
    from evals.tool_use.runner import classify

    provider = ScriptedProvider([_answer("one"), _call("limit", "two")])
    events: list[WispEvent] = []
    result = anyio.run(
        execute_scenario,
        Scenario(steps=(PromptStep(prompt="one"), PromptStep(prompt="two"))),
        _host_factory(tmp_path, monkeypatch, provider, _RecordTool(), max_tool_iterations=0),
        events.append,
    )
    assert result.error is None
    assert result.failed_checks
    metrics = measure(events)
    assert metrics.outcome == "failed"
    assert any(error.startswith("Maximum tool iterations exceeded") for error in metrics.errors)
    assert (
        classify(
            exit_code=0,
            runner_error=result.error,
            metrics=metrics,
            verify_exit=0,
            failed_checks=result.failed_checks,
            scenario_failed=True,
        )
        == "failed"
    )
