"""Measure one run from the JSON events `wisp -p --mode json` wrote."""

from __future__ import annotations

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
    ToolExecutionStarted,
    ToolResultReady,
    wisp_event_from_json,
)
from wisp.mcp.discovery import MCP_CALL


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
    known_usd: Decimal = Decimal(0)
    unpriced_responses: int = 0
    retries: int = 0
    context_overflowed: bool = False
    errors: list[str] = field(default_factory=list)
    proxied_calls: int = 0
    proxied_errors: int = 0
    proxied_argument_errors: int = 0

    @property
    def last_error(self) -> str | None:
        return self.errors[-1] if self.errors else None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

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
            command = event.arguments.get("command")
            if event.name == "bash" and isinstance(command, str):
                metrics.commands.append(command)
        elif isinstance(event, ToolResultReady) and event.is_error:
            metrics.tool_errors[event.failure_code or "unknown"] += 1
            metrics.proxied_errors += event.name == MCP_CALL
            metrics.proxied_argument_errors += (
                event.name == MCP_CALL and event.failure_code == "invalid_arguments"
            )
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


def _add_usage(metrics: RunMetrics, event: MessageCompleted | CompactionCompleted) -> None:
    if event.usage is not None:
        metrics.input_tokens += event.usage.input_tokens
        metrics.output_tokens += event.usage.output_tokens
        metrics.cache_read_tokens += event.usage.cache_read_input_tokens or 0
    if event.cost is not None and event.cost.estimated_usd is not None:
        metrics.known_usd += event.cost.estimated_usd
    elif event.usage is not None:
        metrics.unpriced_responses += 1
