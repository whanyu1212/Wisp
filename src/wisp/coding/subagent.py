"""Run a subagent for one `spawn_agent` call.

A subagent is a second `AgentHarness` on the session's provider. It starts from the
session prompt and the task, or, when forked, from the conversation so far. Its tool
calls run as nested calls of the `spawn_agent` call that started it, through
`ConfiguredToolExecutor.run_nested`: read-only, never asking for approval, recorded
in that call's result. Only its final message goes back to the model that spawned it.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
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
from wisp.messages import Message
from wisp.providers.base import ContextOverflowError, ProviderError
from wisp.providers.events import ToolCall
from wisp.tools.codemode.calls import NestedCallResult
from wisp.tools.result import ToolError
from wisp.tools.subagent import SPAWN_AGENT, SUBAGENT_MAX_TASKS

# Every round ends in one model response and the final message is one more, so the
# responses of every subagent a call runs always fit in the result that accounts
# for them.
SUBAGENT_MAX_TOOL_ITERATIONS = MAX_TOOL_MODEL_RESPONSES // SUBAGENT_MAX_TASKS - 1
# The most subagents one run works at once. Each is its own stream of model
# requests, so more would mostly wait on the provider's rate limit.
SUBAGENT_MAX_PARALLEL = 4

# A number holds where "keep it short" did not: reports ran to twelve thousand
# characters. The limit asked for sits well inside what the tool lets through,
# which is less than its cap when many tasks share one result.
_REPORT_LENGTH = (
    "Write at most 500 words: a longer report is cut off and its end never read. "
    "Give conclusions and the references that support them, not an account of "
    "everything you read, and leave out code the reader can open from a reference."
)

SUBAGENT_INSTRUCTIONS = (
    "You are a subagent. Another agent gave you the task in the next message and "
    "will read only your final message, so that message must be a complete, "
    "self-contained report: answer first, then the evidence, with file paths and "
    f"line numbers. {_REPORT_LENGTH} You can only read and search; do not try to "
    "change anything. You cannot ask questions, so state any assumption you make. "
    "Stop as soon as the task is answered."
)

# A forked subagent keeps the conversation's own prompt, so its instructions arrive
# with its task, as the user message that follows the copied conversation.
_FORK_INSTRUCTIONS = (
    "You are now a subagent forked from the conversation above. The agent in that "
    "conversation continues separately and will read only your final message. Do "
    "not continue its work: do only the task below, then stop. You can only read "
    "and search; every other tool, though listed, is refused. Your final message "
    "must be a complete, self-contained report: answer first, then the evidence, "
    f"with file paths and line numbers. {_REPORT_LENGTH} You cannot ask questions, "
    "so state any assumption you make."
)
_FORKED_CALL_RESULT = (
    "Forked. You are a subagent this call started; your task is in the next message."
)
_UNFORKED_CALL_RESULT = "This call belongs to the main agent and has no result in this fork."

_NO_REPORT = "The subagent finished without a report."
_OVERFLOW = "The subagent ran out of context before it could report."
# What to try next depends on what filled the context: the subagent's own reading,
# or the conversation a fork began with.
FRESH_OVERFLOW_ADVICE = "Give it a narrower task."
FORK_OVERFLOW_ADVICE = (
    "This conversation is too long to fork: use context fresh and put what the "
    "subagent needs in its task."
)

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


def fork_transcript(messages: Sequence[Message], *, started_by: str | None) -> tuple[Message, ...]:
    """Return a conversation as a subagent forked from it starts with it.

    A fork happens while the conversation's last assistant message still waits for
    its tool results, the `spawn_agent` call among them. Every call still waiting
    is given a result here, so the copy is a complete exchange and a provider
    accepts it: the call that forked says the reader is now its subagent, the
    others that they belong to the main agent. Results already there are kept.

    Args:
        messages (Sequence[Message]): The conversation up to the fork, ending in an
            assistant message with tool calls and any results that have arrived.
        started_by (str | None): The id of the `spawn_agent` call that forked, or
            None when it is not known, which marks no call as the subagent's own.

    Returns:
        tuple[Message, ...]: The conversation with every call of that message
        answered, in call order after the results already present. A conversation
        that does not end that way is returned unchanged.
    """

    calls_at = next(
        (index for index in range(len(messages) - 1, -1, -1) if messages[index].role != "tool"),
        None,
    )
    if calls_at is None or not messages[calls_at].tool_calls:
        return tuple(messages)
    answered = {message.tool_call_id for message in messages[calls_at + 1 :]}
    placeholders = [
        Message(
            role="tool",
            content=(
                _FORKED_CALL_RESULT
                if call.name == SPAWN_AGENT and call.call_id == started_by
                else _UNFORKED_CALL_RESULT
            ),
            tool_call_id=call.call_id,
            tool_name=call.name,
        )
        for call in messages[calls_at].tool_calls or ()
        if call.call_id not in answered
    ]
    return (*messages, *placeholders)


def fork_prompt(task: str) -> str:
    """Return the user message that gives a forked subagent its role and its task."""

    return f"{_FORK_INSTRUCTIONS}\n\nTask:\n{task}"


async def run_subagent(
    harness: AgentHarness,
    task: str,
    *,
    record_usage: ModelUsageRecorder,
    overflow_advice: str = FRESH_OVERFLOW_ADVICE,
) -> str:
    """Run a subagent on one task and return its final message.

    Every completed response is reported to ``record_usage`` as it arrives, so the
    subagent's spend is accounted for however it ends. Cancelling the caller
    cancels the subagent; its event stream is closed before this returns or raises.

    Args:
        harness (AgentHarness): The subagent's harness, holding its prompt and, for
            a fork, the conversation it was forked from.
        task (str): The task, sent as the subagent's next user message.
        record_usage (ModelUsageRecorder): Receives each response's usage and cost.
        overflow_advice (str): What the model should try when the subagent runs out
            of context. Defaults to narrowing the task.

    Returns:
        str: The subagent's final message. When it reached its tool-round limit
        first, a note that it did not finish, followed by the last text it wrote.

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
        raise ToolError(f"{_OVERFLOW} {overflow_advice}") from exc
    except ProviderError as exc:
        raise ToolError(f"The subagent failed: {exc}") from exc
    finally:
        with anyio.CancelScope(shield=True):
            await events.aclose()

    if limit_reached:
        limit = harness.config.max_tool_iterations
        note = f"[The subagent stopped at its limit of {limit} tool rounds before finishing.]"
        # First, so the note survives when a long partial report is cut at its end.
        return f"{note}\n\n{last_text}" if last_text else note
    if failure is not None:
        raise ToolError(f"The subagent failed: {failure}")
    if not report:
        raise ToolError(_NO_REPORT)
    return report


__all__ = [
    "FORK_OVERFLOW_ADVICE",
    "FRESH_OVERFLOW_ADVICE",
    "SUBAGENT_INSTRUCTIONS",
    "SUBAGENT_MAX_PARALLEL",
    "SUBAGENT_MAX_TOOL_ITERATIONS",
    "NestedCallToolExecutor",
    "fork_prompt",
    "fork_transcript",
    "run_subagent",
]
