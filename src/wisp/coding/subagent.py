"""Run a subagent for one `spawn_agent` call.

A subagent is a second `AgentHarness` on the session's provider. It starts from the
session prompt and the task, and its tool calls run as nested calls of the
`spawn_agent` call that started it, through `ConfiguredToolExecutor.run_nested`:
read-only, never asking for approval, recorded in that call's result. Only its final
message goes back to the model that spawned it.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace

import anyio
from typing_extensions import TypeAliasType

from wisp.agent.harness import AgentHarness
from wisp.agent.loop import ToolIterationLimitError
from wisp.agent.tool_contracts import (
    PreparedToolExecution,
    ToolExecutionEvent,
    ToolPreparationEvent,
)
from wisp.events import (
    MAX_TOOL_MODEL_RESPONSES,
    ErrorEvent,
    MessageCompleted,
    TokenUsage,
    ToolExecutionEnded,
    UsageCost,
)
from wisp.providers.base import ContextOverflowError, ProviderError
from wisp.providers.events import ToolCall
from wisp.tools.codemode.calls import NestedCallResult
from wisp.tools.result import ToolError
from wisp.tools.subagent import SUBAGENT_MAX_TASKS

# Every round ends in one model response and the final message is one more, so the
# responses of every subagent a call runs always fit in the result that accounts
# for them.
SUBAGENT_MAX_TOOL_ITERATIONS = MAX_TOOL_MODEL_RESPONSES // SUBAGENT_MAX_TASKS - 1
# The most subagents one run works at once. Each is its own stream of model
# requests, so more would mostly wait on the provider's rate limit.
SUBAGENT_MAX_PARALLEL = 4

SUBAGENT_INSTRUCTIONS = (
    "You are a subagent. Another agent gave you the task in the next message and "
    "will read only your final message, so that message must be a complete, "
    "self-contained report: answer first, then the evidence, with file paths and "
    "line numbers. Keep it short: give conclusions and the references that support "
    "them, not an account of everything you read, and leave out code the reader can "
    "open from a reference. You can only read and search; do not try to change "
    "anything. You cannot ask questions, so state any assumption you make. Stop as "
    "soon as the task is answered."
)

_NO_REPORT = "The subagent finished without a report."

NestedCallRunner = TypeAliasType(
    "NestedCallRunner", Callable[[ToolCall], Awaitable[NestedCallResult]]
)
ModelUsageRecorder = TypeAliasType(
    "ModelUsageRecorder", Callable[[TokenUsage, UsageCost | None], None]
)


class NestedCallToolExecutor:
    """Run a subagent's tool calls as nested calls of its `spawn_agent` call.

    The loop sees an ordinary executor. Policy, the read-only rule, approvals and
    scheduling all belong to the nested-call runner, so every prepared call is
    reported parallel-safe and the runner makes the calls that are not take turns.

    Each call is recorded under an id this executor numbers itself. A provider may
    repeat call ids from one response to the next, and the subagents of one
    `spawn_agent` call share its record, where ids must not repeat.
    """

    def __init__(self, run_nested: NestedCallRunner, *, call_id_prefix: str) -> None:
        """Create the executor.

        Args:
            run_nested (NestedCallRunner): Checks, runs and records one nested call;
                see `ConfiguredToolExecutor.run_nested`.
            call_id_prefix (str): Starts every recorded call id, and differs for
                each subagent of a run.
        """

        self._run_nested = run_nested
        self._call_id_prefix = call_id_prefix
        self._call_numbers = itertools.count(1)

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        """Run one call and yield its result.

        Args:
            tool_call (ToolCall): The call the subagent's model made.

        Yields:
            ToolExecutionEvent: The call's only event, its terminal result.
        """

        yield await self._ended(tool_call)

    async def prepare(self, tool_call: ToolCall) -> AsyncIterator[ToolPreparationEvent]:
        """Yield the call ready to run; nothing is checked until it does.

        Args:
            tool_call (ToolCall): The call the subagent's model made.

        Yields:
            ToolPreparationEvent: The prepared call.
        """

        yield PreparedToolExecution(
            call_id=tool_call.call_id,
            name=tool_call.name,
            parallel_safe=True,
            runner=lambda: self._ended(tool_call),
        )

    async def _ended(self, tool_call: ToolCall) -> ToolExecutionEnded:
        recorded_id = f"{self._call_id_prefix}-{next(self._call_numbers)}"
        result = await self._run_nested(replace(tool_call, call_id=recorded_id))
        return ToolExecutionEnded(
            call_id=tool_call.call_id,
            name=tool_call.name,
            output=result.text,
            is_error=result.is_error,
        )


async def run_subagent(
    harness: AgentHarness, task: str, *, record_usage: ModelUsageRecorder
) -> str:
    """Run a subagent on one task and return its final message.

    Every completed response is reported to ``record_usage`` as it arrives, so the
    subagent's spend is accounted for however it ends. Cancelling the caller
    cancels the subagent; its event stream is closed before this returns or raises.

    Args:
        harness (AgentHarness): The subagent's harness, holding its prompt and no
            conversation.
        task (str): The task, sent as the subagent's only user message.
        record_usage (ModelUsageRecorder): Receives each response's usage and cost.

    Returns:
        str: The subagent's final message. When it reached its tool-round limit
        first, the last text it wrote, followed by a note that it did not finish.

    Raises:
        ToolError: The subagent's model or provider failed, its context overflowed,
            or it ended without writing anything.
    """

    # The latest response's text is the report; the latest text of any response is
    # what a subagent stopped mid-task has to show.
    report = ""
    last_text = ""
    failure: str | None = None
    limit_reached = False
    events = harness.prompt(task)
    try:
        async for event in events:
            if isinstance(event, MessageCompleted):
                if event.usage is not None:
                    record_usage(event.usage, event.cost)
                report = event.content
                last_text = event.content or last_text
            elif isinstance(event, ErrorEvent):
                failure = event.message
    except ToolIterationLimitError:
        limit_reached = True
    except ContextOverflowError as exc:
        raise ToolError(
            "The subagent ran out of context before it could report. Give it a narrower task."
        ) from exc
    except ProviderError as exc:
        raise ToolError(f"The subagent failed: {exc}") from exc
    finally:
        with anyio.CancelScope(shield=True):
            await events.aclose()

    if limit_reached:
        limit = harness.config.max_tool_iterations
        note = f"[The subagent stopped at its limit of {limit} tool rounds before finishing.]"
        return f"{last_text}\n\n{note}" if last_text else note
    if failure is not None:
        raise ToolError(f"The subagent failed: {failure}")
    if not report:
        raise ToolError(_NO_REPORT)
    return report


__all__ = [
    "SUBAGENT_INSTRUCTIONS",
    "SUBAGENT_MAX_PARALLEL",
    "SUBAGENT_MAX_TOOL_ITERATIONS",
    "NestedCallToolExecutor",
    "run_subagent",
]
