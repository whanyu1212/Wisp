"""Coding-layer tool registry, policy, and approval adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

from wisp.agent.execution import ToolExecutionEvent
from wisp.events import ToolApprovalRequested, ToolApprovalResolved, ToolExecutionEnded
from wisp.providers.events import ToolCall
from wisp.runtime.registry import ToolRegistry, UnknownToolError
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.base import Tool
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy


class ConfiguredToolExecutor:
    """Adapt Wisp's registry and approval policies to the pure loop contract."""

    def __init__(
        self,
        *,
        registry: ToolRegistry | None,
        context: ToolContext,
        policy: ToolPolicy,
        approval_policy: ToolApprovalPolicy,
    ) -> None:
        self._registry = registry
        self._context = context
        self._policy = policy
        self._approval_policy = approval_policy

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        arguments = dict(tool_call.arguments)
        output: str
        is_error = False
        # Only a real ToolResult carries structured data; the synthetic error
        # paths below (parse error, unconfigured, unknown tool, blocked, denied)
        # leave this empty, which the renderer treats as "nothing structured".
        data: Mapping[str, object] = {}

        if tool_call.parse_error is not None:
            output = tool_call.parse_error
            is_error = True
        elif self._registry is None:
            output = "Tool execution is not configured"
            is_error = True
        else:
            try:
                tool = self._registry.get(tool_call.name)
            except UnknownToolError as exc:
                output = str(exc)
                is_error = True
            else:
                if not self._policy.allows(tool):
                    output = self._policy.block_reason(tool)
                    is_error = True
                elif self._approval_policy.requires_approval(
                    tool
                ) and not self._approval_policy.approves(tool):
                    self._approval_policy.prepare_approval(
                        tool,
                        call_id=tool_call.call_id,
                        arguments=arguments,
                    )
                    yield ToolApprovalRequested(
                        call_id=tool_call.call_id,
                        name=tool_call.name,
                        arguments=arguments,
                        safety=tool.safety,
                    )
                    decision = await self._approval_policy.await_approval(
                        tool,
                        call_id=tool_call.call_id,
                        arguments=arguments,
                    )
                    yield ToolApprovalResolved(
                        call_id=tool_call.call_id,
                        name=tool_call.name,
                        approved=decision.approved,
                        reason=decision.reason,
                    )
                    if decision.approved:
                        output, is_error, data = await self._run_tool(tool, arguments)
                    else:
                        output = decision.reason or "Tool execution was not approved"
                        is_error = True
                else:
                    output, is_error, data = await self._run_tool(tool, arguments)

        yield ToolExecutionEnded(
            call_id=tool_call.call_id,
            name=tool_call.name,
            output=output,
            is_error=is_error,
            data=data,
        )

    async def _run_tool(
        self, tool: Tool, arguments: dict[str, object]
    ) -> tuple[str, bool, Mapping[str, object]]:
        try:
            result = await tool.run(arguments, self._context)
            return result.text, False, result.data
        except Exception as exc:  # noqa: BLE001 - tool failures are model-visible results
            return str(exc), True, {}


__all__ = ["ConfiguredToolExecutor"]
