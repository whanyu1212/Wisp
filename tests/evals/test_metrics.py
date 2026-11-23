"""Measuring a run from its JSON events (`evals.tool_use.metrics`)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import anyio
import pytest

from evals.tool_use.metrics import measure, parse_events, script_failure_reason
from tests.agent.loop.support import tool_call_stream
from wisp.coding.session import CodingSession
from wisp.events import (
    AgentCompleted,
    AgentStarted,
    BillableTokenUsage,
    CompactionCompleted,
    ErrorEvent,
    MessageCompleted,
    ProviderRetrying,
    RunSetup,
    TokenUsage,
    ToolCallSnapshot,
    ToolExecutionEnded,
    ToolExecutionStarted,
    ToolNestedCallStarted,
    ToolResultReady,
    UsageCost,
    UsageCostRates,
    WispEvent,
)
from wisp.providers.events import ProviderResponseCompleted, ProviderResponseStarted, ToolCall
from wisp.providers.fake import ScriptedProvider
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tool_types import ToolFailureCode
from wisp.tools.base import ToolArguments, ToolInputSchema
from wisp.tools.codemode.calls import CODEMODE
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolError, ToolResult

_SETUP = RunSetup(tool_names=("bash",), tools_sha256="a" * 64, instructions_sha256="b" * 64)


def _bash(call_id: str, command: str) -> ToolExecutionStarted:
    return ToolExecutionStarted(call_id=call_id, name="bash", arguments={"command": command})


def _reply(input_tokens: int, usd: str | None) -> MessageCompleted:
    return MessageCompleted(
        turn=1,
        content="",
        finish_reason="stop",
        usage=TokenUsage(
            input_tokens=input_tokens,
            output_tokens=10,
            total_tokens=input_tokens + 10,
            cache_read_input_tokens=5,
        ),
        cost=_cost(usd),
    )


def _cost(usd: str | None) -> UsageCost:
    if usd is None:
        return UsageCost(provider="openai-codex", unavailable_reason="pricing_unavailable")
    return UsageCost(
        provider="openai",
        billable=BillableTokenUsage(
            input_tokens=95, cache_read_input_tokens=5, cache_write_input_tokens=0, output_tokens=10
        ),
        rates=UsageCostRates(input_usd_per_million=Decimal(1), output_usd_per_million=Decimal(1)),
        estimated_usd=Decimal(usd),
    )


def _lines(events: list[WispEvent]) -> list[str]:
    return [event.model_dump_json() for event in events]


def test_measures_run() -> None:
    events: list[WispEvent] = [
        AgentStarted(session_id="s", setup=_SETUP),
        _bash("1", "bash build.sh"),
        ToolResultReady(call_id="1", name="bash", output="", is_error=True, failure_code="timeout"),
        _bash("2", "bash build.sh"),
        ToolExecutionStarted(call_id="3", name="read_output", arguments={"output_id": "o"}),
        ToolResultReady(call_id="3", name="read_output", output="", is_error=True),
        ProviderRetrying(
            turn=1,
            provider="openai",
            attempt=1,
            max_attempts=3,
            delay_seconds=1,
            reason="rate_limit",
        ),
        _reply(100, "0.25"),
        _reply(50, None),
        ErrorEvent(message="first"),
        ErrorEvent(message="last"),
        AgentCompleted(session_id="s", turns=2, outcome="completed"),
    ]

    metrics = measure(parse_events(["", *_lines(events)]))

    assert metrics.setup == _SETUP
    assert (metrics.outcome, metrics.turns) == ("completed", 2)
    assert dict(metrics.tool_calls) == {"bash": 2, "read_output": 1}
    assert dict(metrics.tool_errors) == {"timeout": 1, "unknown": 1}
    assert (metrics.reruns(), metrics.reruns("build"), metrics.reruns("make")) == (1, 1, 0)
    assert (metrics.input_tokens, metrics.output_tokens, metrics.cache_read_tokens) == (150, 20, 10)
    assert (metrics.known_usd, metrics.unpriced_responses) == (Decimal("0.25"), 1)
    assert (metrics.retries, metrics.last_error) == (1, "last")


def test_counts_completed_model_rounds() -> None:
    calls = tuple(ToolCallSnapshot(call_id=name, name="bash", arguments={}) for name in ("1", "2"))
    events: list[WispEvent] = [
        AgentStarted(session_id="a"),
        MessageCompleted(turn=1, content="", finish_reason="tool_calls", tool_calls=calls),
        _bash("1", "true"),
        _bash("2", "true"),
        ToolNestedCallStarted(parent_call_id="1", call_id="nested", name="read"),
        MessageCompleted(turn=2, content="", finish_reason="stop"),
        AgentCompleted(session_id="a", turns=2, outcome="completed"),
        AgentStarted(session_id="a"),
        MessageCompleted(turn=1, content="", finish_reason="stop", role="user"),
        MessageCompleted(turn=1, content="partial", finish_reason="error"),
        MessageCompleted(turn=2, content="partial", finish_reason="cancelled"),
        MessageCompleted(turn=3, content="truncated", finish_reason="length"),
        AgentCompleted(session_id="a", turns=3, outcome="completed"),
    ]

    metrics = measure(parse_events(_lines(events)))

    assert (metrics.model_requests, metrics.tool_rounds) == (3, 1)


def test_unended_run_has_no_outcome() -> None:
    metrics = measure(parse_events(_lines([AgentStarted(session_id="s")])))

    assert (metrics.outcome, metrics.setup) == (None, None)


def test_corrupt_line_rejected() -> None:
    with pytest.raises(ValueError):
        parse_events(['{"type": "agent.started"}'])


def test_counts_compaction_usage() -> None:
    compaction = CompactionCompleted(
        session_id="s",
        reason="threshold",
        outcome="completed",
        replaced_entry_count=4,
        retained_entry_count=1,
        usage=TokenUsage(input_tokens=1000, output_tokens=100, total_tokens=1100),
        cost=_cost("0.5"),
    )

    metrics = measure(parse_events(_lines([_reply(100, None), compaction])))

    assert (metrics.input_tokens, metrics.output_tokens) == (1100, 110)
    assert (metrics.known_usd, metrics.unpriced_responses) == (Decimal("0.5"), 1)


def test_counts_cache_writes() -> None:
    usage = TokenUsage(
        input_tokens=10, output_tokens=5, total_tokens=15, cache_write_input_tokens=300
    )
    reply = MessageCompleted(turn=1, content="", finish_reason="stop", usage=usage)

    metrics = measure([reply, _reply(100, None)])

    assert (metrics.cache_read_tokens, metrics.cache_write_tokens) == (5, 300)


def test_counts_proxied_calls_by_target() -> None:
    events: list[WispEvent] = [
        ToolExecutionStarted(call_id="1", name="mcp_search", arguments={"query": "orders"}),
        ToolExecutionStarted(call_id="2", name="mcp_call", target="mcp__db__q", arguments={}),
        ToolExecutionStarted(call_id="3", name="mcp_call", arguments={"name": "x"}),
        ToolResultReady(
            call_id="3",
            name="mcp_call",
            output="",
            is_error=True,
            failure_code="invalid_arguments",
        ),
    ]

    metrics = measure(parse_events(_lines(events)))

    assert dict(metrics.tool_calls) == {"mcp_search": 1, "mcp__db__q": 1, "mcp_call": 1}
    assert (metrics.proxied_calls, metrics.proxied_errors, metrics.proxied_argument_errors) == (
        2,
        1,
        1,
    )


def test_codemode_counts_scripts() -> None:
    def script(call_id: str) -> ToolExecutionStarted:
        return ToolExecutionStarted(call_id=call_id, name="codemode", arguments={"code": "1"})

    events: list[WispEvent] = [
        script("s1"),
        ToolExecutionEnded(
            call_id="s1",
            name="codemode",
            output="ok",
            is_error=False,
            summary="codemode: 12 tool calls",
        ),
        script("s2"),
        *_nested("s2", 2),
        # A subagent's calls, which no failed script made.
        *_nested("agent", 4),
        _failed_script("s2", "Script failed (runtime): ValueError: stop here\nCalls made"),
        script("s3"),
        ToolExecutionEnded(
            call_id="s3",
            name="codemode",
            output="ok",
            is_error=False,
            summary="codemode: 1 tool call",
        ),
    ]

    metrics = measure(events)

    assert (metrics.scripts, metrics.failed_scripts, metrics.nested_calls) == (3, 1, 13)
    assert metrics.failed_script_calls == 2
    assert metrics.tool_calls["codemode"] == 3
    assert dict(metrics.script_failures) == {"runtime: ValueError": 1}


def _nested(parent_call_id: str, count: int) -> list[ToolNestedCallStarted]:
    return [
        ToolNestedCallStarted(parent_call_id=parent_call_id, call_id=f"{index}", name="read")
        for index in range(count)
    ]


def _failed_script(
    call_id: str, output: str, failure_code: ToolFailureCode | None = None
) -> ToolResultReady:
    return ToolResultReady(
        call_id=call_id, name="codemode", output=output, is_error=True, failure_code=failure_code
    )


@pytest.mark.parametrize(
    ("output", "failure_code", "reason"),
    [
        (
            "Script failed (type): error[unresolved-reference]: Name `ascii` used when not defined",
            None,
            "type: unresolved-reference",
        ),
        ("Script failed (runtime): KeyError: '/Users/me/results/x'", None, "runtime: KeyError"),
        ("Script failed (limit): The script made more than 200 tool calls.", None, "limit"),
        # A message that only looks like an exception adds nothing to another kind.
        ("Script failed (timeout): Note: it did not finish.", None, "timeout"),
        ("code must be a string", "invalid_arguments", "invalid_arguments"),
        ("codemode is unavailable", None, "unknown"),
    ],
    ids=["type", "runtime", "limit", "timeout", "refused", "unclassified"],
)
def test_script_failure_named(
    output: str, failure_code: ToolFailureCode | None, reason: str
) -> None:
    assert script_failure_reason(_failed_script("s", output, failure_code)) == reason


class _ReadTool:
    name = "lookup"
    safety = "read"
    description = "Look a key up."
    input_schema: ClassVar[ToolInputSchema] = {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
    }

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        raise ToolError(f"no such key under {context.cwd}")


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("x = ascii('a')", "type: unresolved-reference"),
        ("raise ValueError('stop here')", "runtime: ValueError"),
        # A tool call that fails inside a helper under `gather` raises in the script.
        (
            "import asyncio\n"
            "async def one(key: str) -> str:\n"
            "    return await lookup(key=key)\n"
            "await asyncio.gather(one('a'), one('b'))",
            "runtime: RuntimeError",
        ),
    ],
    ids=["type", "runtime", "failed-call"],
)
def test_real_script_failure_named(tmp_path: Path, code: str, reason: str) -> None:
    """The reason is read from the text codemode writes, so a real run supplies it."""
    pytest.importorskip("pydantic_monty")
    registry = ToolRegistry()
    registry.register(_ReadTool())
    script = ToolCall(call_id="s", name=CODEMODE, arguments={"code": code})
    done = [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="done")]
    session = CodingSession(
        provider=ScriptedProvider([tool_call_stream(script), done]),
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=registry,
        tool_context=ToolContext(cwd=tmp_path),
        tool_policy=ToolPolicy.allow_all_tools(),
        codemode=True,
    )

    async def run() -> list[WispEvent]:
        return [event async for event in session.run("go")]

    metrics = measure(anyio.run(run))

    assert (metrics.failed_scripts, dict(metrics.script_failures)) == (1, {reason: 1})
