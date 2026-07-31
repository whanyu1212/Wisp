"""Deterministic end-to-end reliability contract for coding-agent workflow."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import anyio

from wisp.agent.execution import ToolExecutionEvent
from wisp.agent.harness import AgentHarness, AgentHarnessConfig
from wisp.agent.prompt import build_prompt_messages
from wisp.events import MessageCompleted, ToolExecutionEnded, ToolResultReady
from wisp.providers.base import ToolSpec
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderToolCallCompleted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider

_BRANCH_NAME = "codex/reliability-eval-175"
_USER_PROMPT = (
    "Refresh origin/main, create the requested feature branch from it, and verify the "
    "change. Report the implementation and verification outcome accurately."
)
_FINAL_SUMMARY = (
    f"Implementation: created `{_BRANCH_NAME}` from refreshed `origin/main` after "
    "confirming local `main` was behind. Verification: the first `uv run pytest` timed "
    "out after 30 seconds and is inconclusive; `uv run pytest "
    "tests/test_agent_loop_core.py` passed with exit code 0; the longer `uv run pytest` "
    "retry passed with exit code 0. Remaining uncertainty: none."
)
_BASH_TOOL = ToolSpec(
    name="bash",
    description="Run a shell command.",
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "number"},
        },
        "required": ["command"],
    },
)


@dataclass(frozen=True)
class _BashOutcome:
    command: str
    timeout: int | None
    output: str
    is_error: bool
    exit_code: int | None


class _ReliabilityScenarioExecutor:
    """Replay the repository state and command outcomes without a shell or network."""

    def __init__(self, outcomes: dict[str, _BashOutcome]) -> None:
        self._outcomes = outcomes
        self.calls: list[ToolCall] = []

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        self.calls.append(tool_call)
        outcome = self._outcomes.get(tool_call.call_id)
        assert outcome is not None, f"Unexpected tool call: {tool_call.call_id}"
        assert tool_call.name == "bash"

        expected_arguments: dict[str, object] = {"command": outcome.command}
        if outcome.timeout is not None:
            expected_arguments["timeout"] = outcome.timeout
        assert dict(tool_call.arguments) == expected_arguments

        yield ToolExecutionEnded(
            call_id=tool_call.call_id,
            name=tool_call.name,
            output=outcome.output,
            is_error=outcome.is_error,
            exit_code=outcome.exit_code,
            output_has_exit_status=outcome.exit_code is not None,
        )


def _bash_call(call_id: str, command: str, *, timeout: int | None = None) -> ToolCall:
    arguments: dict[str, object] = {"command": command}
    if timeout is not None:
        arguments["timeout"] = timeout
    return ToolCall(
        call_id=call_id,
        name="bash",
        arguments=arguments,
        response_id=f"response-{call_id}",
    )


def _tool_call_stream(tool_call: ToolCall) -> tuple[ProviderEvent, ...]:
    return (
        ProviderResponseStarted(model="reliability-eval", response_id=tool_call.response_id),
        ProviderToolCallCompleted(tool_call=tool_call),
        ProviderResponseCompleted(
            content="",
            tool_calls=(tool_call,),
            response_id=tool_call.response_id,
            finish_reason="tool_calls",
        ),
    )


def _completion_stream() -> tuple[ProviderEvent, ...]:
    return (
        ProviderResponseStarted(model="reliability-eval", response_id="response-summary"),
        ProviderResponseCompleted(content=_FINAL_SUMMARY, response_id="response-summary"),
    )


def test_coding_agent_reliability_workflow_handles_branch_timeout_and_completion(
    tmp_path: Path,
) -> None:
    fetch = _bash_call("fetch", "git fetch origin --prune")
    compare_refs = _bash_call("compare-refs", "git rev-parse main origin/main")
    create_branch = _bash_call(
        "create-branch",
        f"git switch -c {_BRANCH_NAME} origin/main",
    )
    initial_full_test = _bash_call("full-test-timeout", "uv run pytest", timeout=30)
    focused_test = _bash_call(
        "focused-test",
        "uv run pytest tests/test_agent_loop_core.py",
        timeout=30,
    )
    full_test_retry = _bash_call("full-test-retry", "uv run pytest", timeout=300)
    tool_calls = (
        fetch,
        compare_refs,
        create_branch,
        initial_full_test,
        focused_test,
        full_test_retry,
    )

    outcomes = {
        fetch.call_id: _BashOutcome(
            command="git fetch origin --prune",
            timeout=None,
            output="Command exited with code 0\nFetched origin.",
            is_error=False,
            exit_code=0,
        ),
        compare_refs.call_id: _BashOutcome(
            command="git rev-parse main origin/main",
            timeout=None,
            output=(
                "Command exited with code 0\n"
                "1111111 local main\n"
                "2222222 origin/main\n"
                "local main is behind origin/main"
            ),
            is_error=False,
            exit_code=0,
        ),
        create_branch.call_id: _BashOutcome(
            command=f"git switch -c {_BRANCH_NAME} origin/main",
            timeout=None,
            output=(f"Command exited with code 0\nCreated {_BRANCH_NAME} from origin/main."),
            is_error=False,
            exit_code=0,
        ),
        initial_full_test.call_id: _BashOutcome(
            command="uv run pytest",
            timeout=30,
            output="Command timed out after 30 seconds",
            is_error=True,
            exit_code=None,
        ),
        focused_test.call_id: _BashOutcome(
            command="uv run pytest tests/test_agent_loop_core.py",
            timeout=30,
            output="Command exited with code 0\n37 passed",
            is_error=False,
            exit_code=0,
        ),
        full_test_retry.call_id: _BashOutcome(
            command="uv run pytest",
            timeout=300,
            output="Command exited with code 0\n2042 passed, 1 skipped",
            is_error=False,
            exit_code=0,
        ),
    }
    provider = ScriptedProvider(
        [*(_tool_call_stream(tool_call) for tool_call in tool_calls), _completion_stream()]
    )
    executor = _ReliabilityScenarioExecutor(outcomes)
    harness = AgentHarness(
        AgentHarnessConfig(
            provider=provider,
            tool_executor=executor,
            tools=(_BASH_TOOL,),
        ),
        messages=build_prompt_messages(
            cwd=tmp_path,
            tools=(_BASH_TOOL,),
            include_project_context=False,
        ),
    )

    async def run() -> list[object]:
        return [event async for event in harness.prompt(_USER_PROMPT)]

    events = anyio.run(run)

    system_prompt = " ".join(provider.calls[0].messages[0].content.split())
    assert "fetch the relevant remote and compare refs" in system_prompt
    assert "A timeout is inconclusive, never a pass" in system_prompt
    assert "remaining blockers or uncertainty" in system_prompt
    assert provider.calls[0].messages[-1].content == _USER_PROMPT
    assert provider.calls[0].tools == (_BASH_TOOL,)
    assert executor.calls == list(tool_calls)

    tool_results_by_call = {
        event.call_id: event for event in events if isinstance(event, ToolResultReady)
    }
    timeout_result = tool_results_by_call[initial_full_test.call_id]
    assert timeout_result.is_error is True
    assert timeout_result.exit_code is None
    assert timeout_result.output_has_exit_status is False
    assert timeout_result.output == "Command timed out after 30 seconds"
    retry_result = tool_results_by_call[full_test_retry.call_id]
    assert retry_result.is_error is False
    assert retry_result.exit_code == 0
    assert retry_result.output_has_exit_status is True

    provider_results = [result for request in provider.calls[1:] for result in request.tool_results]
    assert [(result.call_id, result.is_error) for result in provider_results] == [
        (fetch.call_id, False),
        (compare_refs.call_id, False),
        (create_branch.call_id, False),
        (initial_full_test.call_id, True),
        (focused_test.call_id, False),
        (full_test_retry.call_id, False),
    ]
    assert provider_results[3].output == "Command timed out after 30 seconds"

    final_message = [event for event in events if isinstance(event, MessageCompleted)][-1]
    assert final_message.content == _FINAL_SUMMARY
    assert "timed out after 30 seconds and is inconclusive" in final_message.content
    assert "retry passed with exit code 0" in final_message.content
    assert "Remaining uncertainty: none." in final_message.content
