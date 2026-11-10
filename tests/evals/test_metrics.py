"""Measuring a run from its JSON events (`evals.tool_use.metrics`)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from evals.tool_use.metrics import measure, parse_events
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
    ToolExecutionEnded,
    ToolExecutionStarted,
    ToolResultReady,
    UsageCost,
    UsageCostRates,
    WispEvent,
)

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
        ToolResultReady(call_id="s2", name="codemode", output="Script failed", is_error=True),
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
    assert metrics.tool_calls["codemode"] == 3
