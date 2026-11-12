"""The `spawn_agent` tool, which hands one bounded task to a subagent.

The tool only describes the call. A coding session supplies the function that runs
the subagent, because that needs the session's provider, prompt and executor; see
`wisp.coding.subagent`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from typing_extensions import TypeAliasType

from wisp.tool_types import ToolSafety
from wisp.tools.base import ToolArguments, ToolInputSchema, ToolPromptMetadata
from wisp.tools.common import _required_string
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolResult

SPAWN_AGENT = "spawn_agent"

# Runs a subagent on a task and returns its final message. It raises `ToolError`
# when the subagent fails, so the model reads why.
SubagentRunner = TypeAliasType("SubagentRunner", Callable[[str], Awaitable[str]])

SPAWN_AGENT_PROMPT = ToolPromptMetadata(
    prompt_snippet="spawn_agent: hand one read-only task to a subagent and get its report.",
    guidelines=(
        "Do the work yourself by default. Use spawn_agent only for a broad search or "
        "investigation whose raw output would crowd your context; read a file or "
        "symbol you can already name directly.",
        "A subagent sees none of this conversation: put everything it needs in the "
        "task, and say what to report back.",
    ),
)


class SpawnAgentTool:
    """Run a read-only subagent on one task and return its final message.

    The subagent starts from the task alone, with none of the conversation. It may
    use the run's read-only tools, and cannot spawn subagents itself. Nothing it
    reads reaches the caller except its final message.
    """

    name = SPAWN_AGENT
    description = (
        "Hand one bounded, read-only task to a subagent and get back its final report. "
        "The subagent starts fresh: it sees only the task you write, not this "
        "conversation, and it can only read and search. Use it rarely, for a broad "
        "search or investigation whose raw output you do not need to see yourself. "
        "It costs extra model calls, so prefer your own tools for anything you can "
        "locate directly."
    )
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "The complete task: what to find out, where to look, what is already "
                    "known, and what the report should contain."
                ),
            }
        },
        "required": ["task"],
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
        """Run a subagent on the task and return its final message.

        Args:
            arguments (ToolArguments): The call's arguments; `task` is required.
            context (ToolContext): Unused; the subagent runs with the context its
                runner was built with.

        Returns:
            ToolResult: The subagent's final message.

        Raises:
            ToolArgumentError: `task` is missing or empty.
            ToolError: The subagent failed before it could report.
        """

        task = _required_string(arguments, "task")
        return ToolResult(text=await self._run_subagent(task))


__all__ = ["SPAWN_AGENT", "SPAWN_AGENT_PROMPT", "SpawnAgentTool", "SubagentRunner"]
