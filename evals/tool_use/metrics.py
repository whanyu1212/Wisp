"""Measure one run from the JSON events `wisp -p --mode json` wrote."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from wisp.events import (
    AgentCompleted,
    AgentStarted,
    CompactionCompleted,
    ContextOverflow,
    ErrorEvent,
    KnownWispEvent,
    MessageCompleted,
    ProviderRetrying,
    RunOutcome,
    RunSetup,
    ToolExecutionEnded,
    ToolExecutionStarted,
    ToolResultReady,
    wisp_event_from_json,
)
from wisp.mcp.discovery import MCP_CALL
from wisp.tools.codemode.calls import CODEMODE

# The codemode result's summary, as `tools/summary.py` writes it.
_NESTED_CALLS = re.compile(r"^codemode: (\d+) tool calls?\b")
# A failed script's result, as `tools/codemode/tool.py` heads it: the failure kind,
# then the type checker's diagnostic code or the exception's class.
_SCRIPT_FAILED = re.compile(r"^Script failed \((\w+)\): (?:error\[([\w-]+)\]|([A-Za-z_]\w*):)?")


@dataclass(slots=True)
class RunMetrics:
    """What a run did, from its events alone.

    Attributes:
        setup (RunSetup | None): The setup from `agent.started`.
        outcome (RunOutcome | None): The `agent.completed` outcome, or None when
            the run ended without one (a crash, or a forced stop).
        turns (int | None): Turns the run took, from `agent.completed`.
        tool_calls (Counter[str]): Calls started, by tool name.
        tool_errors (Counter[str]): Failed tool results, by `failure_code`
            (`unknown` when the result names none).
        commands (list[str]): Every `bash` command started, in order.
        input_tokens (int): Input tokens over all model responses.
        output_tokens (int): Output tokens over all model responses.
        cache_read_tokens (int): Input tokens served from the provider's cache.
        cache_write_tokens (int): Input tokens written to the provider's cache.
        known_usd (Decimal): Estimated cost of the priced responses.
        unpriced_responses (int): Responses without a cost estimate.
        retries (int): Provider retries (rate limits, transient errors).
        context_overflowed (bool): Whether the conversation outgrew the model's
            context window.
        errors (list[str]): Every `error` event's message, in order.
        proxied_calls (int): On-demand `mcp_call` calls. Each also counts in
            ``tool_calls`` under the tool it resolved to, or as `mcp_call` when
            the name it gave matched no catalog tool.
        proxied_errors (int): `mcp_call` calls that failed for any reason: Wisp's
            pre-check, an unknown name, or the server rejecting them. The measure
            of whether proxied calls are harder to get right than direct ones.
        proxied_argument_errors (int): The subset Wisp's pre-check rejected
            because a required parameter was missing or arguments were not an object.
        scripts (int): `codemode` calls. Each counts once in ``tool_calls``, however
            many calls its script made.
        failed_scripts (int): `codemode` calls that failed: a script that did not
            type-check, raised, or hit a limit.
        script_failures (Counter[str]): Those failures by reason, as
            `script_failure_reason` names them. They add up to ``failed_scripts``.
        nested_calls (int): Tool calls the scripts that finished made.
    """

    setup: RunSetup | None = None
    outcome: RunOutcome | None = None
    turns: int | None = None
    tool_calls: Counter[str] = field(default_factory=Counter)
    tool_errors: Counter[str] = field(default_factory=Counter)
    commands: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    known_usd: Decimal = Decimal(0)
    unpriced_responses: int = 0
    retries: int = 0
    context_overflowed: bool = False
    errors: list[str] = field(default_factory=list)
    proxied_calls: int = 0
    proxied_errors: int = 0
    proxied_argument_errors: int = 0
    scripts: int = 0
    failed_scripts: int = 0
    script_failures: Counter[str] = field(default_factory=Counter)
    nested_calls: int = 0

    @property
    def last_error(self) -> str | None:
        return self.errors[-1] if self.errors else None

    def reruns(self, fragment: str | None = None) -> int:
        """Count `bash` commands repeated within the run.

        Args:
            fragment (str | None): Count only commands containing this text.

        Returns:
            int: Runs of a command beyond its first.
        """

        counts = Counter(
            command for command in self.commands if fragment is None or fragment in command
        )
        return sum(count - 1 for count in counts.values())


def parse_events(lines: Iterable[str]) -> list[KnownWispEvent]:
    """Parse JSON event lines, skipping blank ones.

    Raises:
        ValueError: When a line is not a valid Wisp event. Runner and Wisp are
            the same release, so this means the output is corrupt.
    """

    return [wisp_event_from_json(line) for line in lines if line.strip()]


def measure(events: Iterable[KnownWispEvent]) -> RunMetrics:
    """Summarize a run's events into `RunMetrics`."""

    metrics = RunMetrics()
    for event in events:
        if isinstance(event, AgentStarted):
            metrics.setup = event.setup
        elif isinstance(event, AgentCompleted):
            metrics.outcome = event.outcome
            metrics.turns = event.turns
        elif isinstance(event, ToolExecutionStarted):
            # A proxied call counts under the tool it ran, so modes compare like with like.
            metrics.tool_calls[event.target or event.name] += 1
            metrics.proxied_calls += event.name == MCP_CALL
            metrics.scripts += event.name == CODEMODE
            command = event.arguments.get("command")
            if event.name == "bash" and isinstance(command, str):
                metrics.commands.append(command)
        elif isinstance(event, ToolResultReady) and event.is_error:
            metrics.tool_errors[event.failure_code or "unknown"] += 1
            metrics.proxied_errors += event.name == MCP_CALL
            metrics.proxied_argument_errors += (
                event.name == MCP_CALL and event.failure_code == "invalid_arguments"
            )
            if event.name == CODEMODE:
                metrics.failed_scripts += 1
                metrics.script_failures[script_failure_reason(event)] += 1
        elif isinstance(event, ToolExecutionEnded) and event.name == CODEMODE:
            match = _NESTED_CALLS.match(event.summary or "")
            metrics.nested_calls += int(match.group(1)) if match else 0
        elif isinstance(event, MessageCompleted | CompactionCompleted):
            # Compaction summarizes the conversation with its own paid request.
            _add_usage(metrics, event)
        elif isinstance(event, ProviderRetrying):
            metrics.retries += 1
        elif isinstance(event, ContextOverflow):
            metrics.context_overflowed = True
        elif isinstance(event, ErrorEvent):
            metrics.errors.append(event.message)
    return metrics


def script_failure_reason(result: ToolResultReady) -> str:
    """Name why a `codemode` call failed, without quoting its error text.

    The reason is safe to commit in a baseline: it comes from a fixed vocabulary,
    while the error text can hold paths under the folder a run was made in.

    Args:
        result (ToolResultReady): The failed `codemode` result.

    Returns:
        str: The failure kind the script result names (`syntax`, `type`, `runtime`,
        `limit`, `timeout` or `unavailable`). A `type` failure adds the type
        checker's first diagnostic code and a `runtime` one the exception's class,
        as in `type: unresolved-reference` and `runtime: ValueError`. A call refused
        before any script ran gives its `failure_code`, or `unknown` without one.
    """

    match = _SCRIPT_FAILED.match(result.output)
    if match is None:
        return result.failure_code or "unknown"
    kind, diagnostic, exception = match.groups()
    detail = diagnostic if kind == "type" else exception if kind == "runtime" else None
    return f"{kind}: {detail}" if detail else kind


def _add_usage(metrics: RunMetrics, event: MessageCompleted | CompactionCompleted) -> None:
    if event.usage is not None:
        metrics.input_tokens += event.usage.input_tokens
        metrics.output_tokens += event.usage.output_tokens
        metrics.cache_read_tokens += event.usage.cache_read_input_tokens or 0
        metrics.cache_write_tokens += event.usage.cache_write_input_tokens or 0
    if event.cost is not None and event.cost.estimated_usd is not None:
        metrics.known_usd += event.cost.estimated_usd
    elif event.usage is not None:
        metrics.unpriced_responses += 1
