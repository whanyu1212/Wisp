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

# Tools whose ``ToolResult.data["exit_code"]`` carries genuine process
# exit-status semantics. Gating promotion by name keeps a custom tool that
# happens to stash an ``exit_code`` in ``data`` from being reddened as a failure
# — only these tools' exit codes reach the event and drive card styling.
_EXIT_CODE_TOOLS = frozenset({"bash"})

# Tools whose ``ToolResult.data["before_text"]`` carries a pre-write snapshot the
# TUI renders as a before/after diff. Gated by name (like _EXIT_CODE_TOOLS) so an
# extension tool that stashes an unrelated ``before_text`` can't inject content into
# the diff renderer — only these tools' snapshots reach the event.
_BEFORE_TEXT_TOOLS = frozenset({"write"})


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
        # Promoted from a real ToolResult for shell-like tools; None for the
        # synthetic error paths below (parse error, unconfigured, unknown tool,
        # blocked, denied) and for tools without exit-code semantics.
        exit_code: int | None = None
        # Pre-write snapshot for the diff renderer; None for every non-write tool and
        # for the synthetic/error paths, which never produce a ToolResult.
        before_text: str | None = None
        # Whether a write created a new file (vs. overwrote an existing one). Lets the
        # renderer show a create as pure additions but fall back to the plain summary
        # for an overwrite whose prior text couldn't be snapshotted. False for every
        # non-write tool and every error path.
        created = False

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
                        output, is_error, exit_code, before_text, created = await self._run_tool(
                            tool, arguments
                        )
                    else:
                        output = decision.reason or "Tool execution was not approved"
                        is_error = True
                else:
                    output, is_error, exit_code, before_text, created = await self._run_tool(
                        tool, arguments
                    )

        yield ToolExecutionEnded(
            call_id=tool_call.call_id,
            name=tool_call.name,
            output=output,
            is_error=is_error,
            exit_code=exit_code,
            before_text=before_text,
            created=created,
        )

    async def _run_tool(
        self, tool: Tool, arguments: dict[str, object]
    ) -> tuple[str, bool, int | None, str | None, bool]:
        try:
            result = await tool.run(arguments, self._context)
            return (
                result.text,
                False,
                _promote_exit_code(tool.name, result.data),
                _promote_before_text(tool.name, result.data),
                _promote_created(tool.name, result.data),
            )
        except Exception as exc:  # noqa: BLE001 - tool failures are model-visible results
            return str(exc), True, None, None, False


def _promote_exit_code(name: str, data: Mapping[str, object]) -> int | None:
    """Extract a process exit code from a tool result, for shell-like tools only.

    Gated on ``_EXIT_CODE_TOOLS`` so an extension tool that stashes an unrelated
    ``exit_code`` in its ``data`` can't drive failure styling. Returns None unless
    a recognized tool reported an integer exit code.
    """

    if name not in _EXIT_CODE_TOOLS:
        return None
    exit_code = data.get("exit_code")
    return exit_code if isinstance(exit_code, int) else None


def _promote_before_text(name: str, data: Mapping[str, object]) -> str | None:
    """Extract a pre-write snapshot from a tool result, for write-like tools only.

    Gated on ``_BEFORE_TEXT_TOOLS`` so an extension tool that stashes an unrelated
    ``before_text`` in its ``data`` can't feed content into the diff renderer. The
    tool already bounds the snapshot; returns None unless a recognized tool reported
    a string.
    """

    if name not in _BEFORE_TEXT_TOOLS:
        return None
    before_text = data.get("before_text")
    return before_text if isinstance(before_text, str) else None


def _promote_created(name: str, data: Mapping[str, object]) -> bool:
    """Whether a write created a new file, for write-like tools only.

    Gated on ``_BEFORE_TEXT_TOOLS`` (the same write-like set) so the flag travels
    only with the snapshot it disambiguates. Returns False unless a recognized tool
    reported a boolean ``created`` — so a missing/odd value defaults to "overwrote",
    the conservative choice that never fabricates a create-style diff.
    """

    if name not in _BEFORE_TEXT_TOOLS:
        return False
    created = data.get("created")
    return created if isinstance(created, bool) else False


__all__ = ["ConfiguredToolExecutor"]
