"""The `spawn_agent` tool, which hands bounded tasks to subagents.

The tool only describes the call and runs its tasks side by side. A coding session
supplies the function that runs one subagent, because that needs the session's
provider, prompt and executor; see `wisp.coding.subagent`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import anyio
from typing_extensions import TypeAliasType

from wisp.tool_types import ToolSafety
from wisp.tools.base import ToolArguments, ToolInputSchema, ToolPromptMetadata
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolArgumentError, ToolError, ToolResult
from wisp.tools.truncation import truncate_text

_logger = logging.getLogger(__name__)

SPAWN_AGENT = "spawn_agent"
# The most tasks one call may hand out. A result accounts for every model response
# its subagents make, which bounds how many there can be; see
# `wisp.events.MAX_TOOL_MODEL_RESPONSES`.
SUBAGENT_MAX_TASKS = 8

# Runs a subagent on a task and returns its final message. It raises `ToolError`
# when the subagent fails, so the model reads why.
SubagentRunner = TypeAliasType("SubagentRunner", Callable[[str], Awaitable[str]])

# What a task's heading and the blank line after its report take from its share of
# the result, in bytes and in lines.
_SECTION_BYTES = 16
_SECTION_LINES = 3

SPAWN_AGENT_PROMPT = ToolPromptMetadata(
    prompt_snippet="spawn_agent: hand read-only tasks to subagents and get their reports.",
    guidelines=(
        "Do the work yourself by default. Use spawn_agent only for a broad search or "
        "investigation whose raw output would crowd your context; read a file or "
        "symbol you can already name directly.",
        "A subagent sees none of this conversation: put everything it needs in its "
        "task, and say what to report back.",
        "Put every independent task in one spawn_agent call. Each task gets its own "
        "subagent and they run in parallel, where separate calls can run one after "
        "another. You continue only after all of them have reported, so do not plan "
        "to work alongside them.",
    ),
)


class SpawnAgentTool:
    """Run a read-only subagent on each task and return their final messages.

    A subagent starts from its task alone, with none of the conversation. It may use
    the run's read-only tools, and cannot spawn subagents itself. Nothing it reads
    reaches the caller except its final message. The tasks of one call run at the
    same time, whatever else the model called beside it.
    """

    name = SPAWN_AGENT
    description = (
        "Hand one or more bounded, read-only tasks to subagents and get back their "
        "final reports. Each task gets its own subagent, which starts fresh: it sees "
        "only the task you write, not this conversation, and it can only read and "
        "search. Use it rarely, for a broad search or investigation whose raw output "
        "you do not need to see yourself. It costs extra model calls, so prefer your "
        "own tools for anything you can locate directly. Pass every independent task "
        "in `tasks` in a single call: the subagents run in parallel and you get all "
        "the reports back together."
    )
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    f"One to {SUBAGENT_MAX_TASKS} tasks, one complete task per "
                    "subagent: what to find out, where to look, what is already known, "
                    "and what the report should contain. Tasks must not depend on each "
                    "other's results."
                ),
            }
        },
        "required": ["tasks"],
        "additionalProperties": False,
    }
    safety: ToolSafety = "read"

    def __init__(self, run_subagent: SubagentRunner) -> None:
        """Create the tool.

        Args:
            run_subagent (SubagentRunner): Runs a subagent on a task and returns its
                final message.
        """

        self._run_subagent = run_subagent

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        """Run one subagent per task, all at once, and return their final messages.

        A subagent that fails does not stop the others, whatever it failed with:
        its part of the result says why, or that it failed unexpectedly, which is
        logged. Cancelling the call cancels every subagent still running.

        Args:
            arguments (ToolArguments): The call's arguments; `tasks` is required.
            context (ToolContext): Gives the result's size limit, which several
                reports share equally. A subagent itself runs with the context its
                runner was built with.

        Returns:
            ToolResult: The only subagent's final message, or each subagent's under
            a `## Task N` heading in the order the tasks were given. A report longer
            than its share of the limit is cut and marked, so a long one never
            costs a later task its place in the result.

        Raises:
            ToolArgumentError: `tasks` is not a list of one to
                `SUBAGENT_MAX_TASKS` non-empty strings.
            ToolError: Every subagent failed before it could report.
        """

        tasks = _tasks(arguments)
        reports: dict[int, str] = {}
        failures: dict[int, ToolError] = {}

        async def work(index: int, task: str) -> None:
            try:
                reports[index] = await self._run_subagent(task)
            except ToolError as exc:
                failures[index] = exc
            except Exception:  # noqa: BLE001 - one subagent's failure must not cancel the rest
                _logger.exception("Subagent %d of %d failed unexpectedly", index + 1, len(tasks))
                failures[index] = ToolError(
                    "The subagent failed unexpectedly.", failure_code="internal_error"
                )

        async with anyio.create_task_group() as subagents:
            for index, task in enumerate(tasks):
                subagents.start_soon(work, index, task)

        if len(tasks) == 1:
            if failures:
                raise failures[0]
            return ToolResult(text=reports[0])
        shares = [
            truncate_text(
                reports.get(index) or str(failures[index]),
                max_bytes=max(context.max_output_bytes // len(tasks) - _SECTION_BYTES, 0),
                max_lines=max(context.max_output_lines // len(tasks) - _SECTION_LINES, 0),
            )
            for index in range(len(tasks))
        ]
        text = "\n\n".join(
            f"## Task {index + 1}\n{share.text}" for index, share in enumerate(shares)
        )
        if not reports:
            raise ToolError(text)
        return ToolResult(text=text, truncated=any(share.truncated for share in shares))


def _tasks(arguments: ToolArguments) -> list[str]:
    """Return the call's tasks, or raise a model-facing error for a malformed list."""

    tasks = arguments.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ToolArgumentError("tasks must be a list with at least one task")
    if len(tasks) > SUBAGENT_MAX_TASKS:
        raise ToolArgumentError(f"tasks must hold at most {SUBAGENT_MAX_TASKS} tasks")
    if not all(isinstance(task, str) and task.strip() for task in tasks):
        raise ToolArgumentError("every task must be a non-empty string")
    return tasks


__all__ = [
    "SPAWN_AGENT",
    "SPAWN_AGENT_PROMPT",
    "SUBAGENT_MAX_TASKS",
    "SpawnAgentTool",
    "SubagentRunner",
]
