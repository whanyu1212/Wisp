"""Coding-layer tool registry, policy, and approval adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass

from wisp.agent.execution import ToolExecutionEvent
from wisp.events import ToolApprovalRequested, ToolApprovalResolved, ToolExecutionEnded
from wisp.providers.events import ToolCall
from wisp.runtime.registry import ToolRegistry, UnknownToolError
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.base import Tool
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.summary import summarize_tool_result

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


@dataclass(frozen=True)
class _ToolRunOutcome:
    """What running one tool produced, before it becomes a ToolExecutionEnded.

    Bundles the output text with the narrow, JSON-safe scalars the executor promotes
    from the structured ``ToolResult.data`` for the TUI — each gated to the tools it
    applies to. The synthetic/error paths (parse error, unconfigured, blocked,
    denied, raised) build one with just ``output``/``is_error`` and every promoted
    field defaulted, so the presentation signals are absent exactly when there was no
    real ToolResult to promote them from.
    """

    output: str
    is_error: bool = False
    exit_code: int | None = None
    before_text: str | None = None
    created: bool = False
    summary: str | None = None
    truncated: bool = False


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
        # The synthetic paths below (parse error, unconfigured, unknown tool, blocked,
        # denied) never produce a ToolResult, so they build an outcome from just the
        # message + is_error and leave every promoted signal at its default.
        outcome: _ToolRunOutcome

        if tool_call.parse_error is not None:
            outcome = _ToolRunOutcome(tool_call.parse_error, is_error=True)
        elif self._registry is None:
            outcome = _ToolRunOutcome("Tool execution is not configured", is_error=True)
        else:
            try:
                tool = self._registry.get(tool_call.name)
            except UnknownToolError as exc:
                outcome = _ToolRunOutcome(str(exc), is_error=True)
            else:
                if not self._policy.allows(tool):
                    outcome = _ToolRunOutcome(self._policy.block_reason(tool), is_error=True)
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
                        outcome = await self._run_tool(tool, arguments)
                    else:
                        outcome = _ToolRunOutcome(
                            decision.reason or "Tool execution was not approved",
                            is_error=True,
                        )
                else:
                    outcome = await self._run_tool(tool, arguments)

        yield ToolExecutionEnded(
            call_id=tool_call.call_id,
            name=tool_call.name,
            output=outcome.output,
            is_error=outcome.is_error,
            exit_code=outcome.exit_code,
            before_text=outcome.before_text,
            created=outcome.created,
            summary=outcome.summary,
            truncated=outcome.truncated,
        )

    async def _run_tool(self, tool: Tool, arguments: dict[str, object]) -> _ToolRunOutcome:
        # Reading result.text/result.data stays inside the try: a tool may return a
        # result object whose fields raise on access (a malformed extension tool), and
        # that must degrade to a model-visible error like any other tool failure, not
        # crash the loop.
        try:
            result = await tool.run(arguments, self._context)
            return _ToolRunOutcome(
                output=result.text,
                exit_code=_promote_exit_code(tool.name, result.data),
                before_text=_promote_before_text(tool.name, result.data),
                created=_promote_created(tool.name, result.data),
                summary=summarize_tool_result(tool.name, result.data, truncated=result.truncated),
                # The tool's own authoritative "I capped my output" flag, so the card
                # can be honest that an expanded view may still not be the whole story.
                # Only a real ToolResult sets this; every synthetic/error path defaults
                # it False.
                truncated=result.truncated,
            )
        except Exception as exc:  # noqa: BLE001 - tool failures are model-visible results
            return _ToolRunOutcome(str(exc), is_error=True)


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
