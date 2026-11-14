"""Coding-layer tool registry, policy, and approval adapter."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import cast

import anyio
from pydantic import ValidationError
from typing_extensions import TypeAliasType

from wisp.agent.tool_contracts import (
    PreparedToolExecution,
    ToolExecutionEvent,
    ToolPreparationEvent,
)
from wisp.coding.tool_progress import LiveEventSink, run_with_progress
from wisp.events import (
    MAX_EDIT_LOCATIONS,
    MAX_NESTED_CALLS,
    MAX_TOOL_RESULT_IMAGES,
    NESTED_CALL_ARGUMENTS_MAX_CHARS,
    NESTED_CALL_TEXT_MAX_CHARS,
    EditLocation,
    ImagePart,
    JsonObject,
    ManagedProcessState,
    ModelResponseUsage,
    NestedToolCall,
    TokenUsage,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolExecutionEnded,
    ToolNestedCallEnded,
    ToolNestedCallStarted,
    UsageCost,
    WispEvent,
    utc_now,
)
from wisp.images import ImageRejectedError
from wisp.mcp.discovery import MCP_CALL, McpCatalog, check_proxied_arguments, resolve_target
from wisp.providers.events import ToolCall
from wisp.runtime.registry import ToolRegistry, UnknownToolError
from wisp.sessions.errors import SessionError
from wisp.tool_types import ToolFailureCode
from wisp.tools.approval import ToolApprovalPolicy, confirms_every_call
from wisp.tools.base import ArgumentCheckedTool, Tool
from wisp.tools.codemode.calls import CODEMODE, NestedCallResult
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.progress import ToolProgress
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.subagent import SPAWN_AGENT
from wisp.tools.summary import summarize_tool_result
from wisp.tools.truncation import truncate_text

_logger = logging.getLogger(__name__)

ImageStore = TypeAliasType("ImageStore", Callable[[bytes], Awaitable[ImagePart]])
"""Validates an image and makes it durable, returning the reference to record."""

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

_MODEL_VISIBLE_TOOL_ERROR_MAX_CHARS = 2_000
# The batch limit, `_MAX_PARALLEL_TOOL_EXECUTIONS` in the loop, for a script's calls.
_MAX_PARALLEL_NESTED_CALLS = 8
# How a nested call is recorded when its script ended before the call finished.
_SCRIPT_ENDED_FIRST = "The script ended before this call finished"
_SUBAGENT_ENDED_FIRST = "The subagent ended before this call finished"
# Appended to a result whose image the reader does not get, so it is never taken for
# one that was seen.
_IMAGE_NOT_ATTACHED = "[The image could not be attached to this result.]"
_MAX_RESULT_COUNT = (1 << 63) - 1
_MIN_EXIT_CODE = -(1 << 31)
_MAX_EXIT_CODE = (1 << 31) - 1
_MAX_PROCESS_ID_BYTES = 256
_MAX_PROCESS_ERROR_BYTES = 2_000
_TRUNCATION_SUFFIX_WITH_SEPARATOR_BYTES = len("\n[truncated]")
_FAILURE_CODES: frozenset[ToolFailureCode] = frozenset(
    (
        "approval_denied",
        "internal_error",
        "invalid_arguments",
        "invalid_pattern",
        "invalid_result",
        "not_found",
        "path_outside_workspace",
        "policy_denied",
        "stale_input",
        "timeout",
        "unknown_tool",
    )
)
_PROCESS_STATES: frozenset[ManagedProcessState] = frozenset(
    ("running", "completed", "failed", "timed_out", "cancelled")
)
_RESULT_DATA_KEYS = {
    "bash": (
        "exit_code",
        "output_has_exit_status",
        "process_id",
        "process_state",
        "process_error",
        "stdout",
        "stderr",
        "stdout_truncated",
        "stderr_truncated",
        "stdout_dropped_bytes",
        "stderr_dropped_bytes",
        "process_started_at",
        "process_ended_at",
    ),
    "write": ("before_text", "created"),
    "edit": ("locations",),
    "read": ("line_count", "selected_count", "path"),
    "grep": ("count",),
    "find": ("count",),
    "ls": ("entries", "entry_count", "path"),
    CODEMODE: ("calls", "failed_calls"),
}


class ToolResultProcessingError(RuntimeError):
    """Raised when Wisp cannot normalize an otherwise returned tool result."""

    def __init__(self, *, call_id: str, tool_name: str) -> None:
        self.call_id = call_id
        self.tool_name = tool_name
        super().__init__("Internal error while processing a tool result")


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
    failure_code: ToolFailureCode | None = None
    retryable: bool = False
    recovery_hint: str | None = None
    exit_code: int | None = None
    output_has_exit_status: bool = False
    before_text: str | None = None
    created: bool = False
    edit_locations: tuple[EditLocation, ...] = ()
    summary: str | None = None
    truncated: bool = False
    process_id: str | None = None
    process_state: ManagedProcessState | None = None
    process_error: str | None = None
    stdout: str | None = None
    stderr: str | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_dropped_bytes: int = 0
    stderr_dropped_bytes: int = 0
    process_started_at: datetime | None = None
    process_ended_at: datetime | None = None
    images: tuple[ImagePart, ...] = ()
    nested_calls: tuple[NestedToolCall, ...] = ()
    nested_calls_omitted: int = 0
    model_usage: tuple[ModelResponseUsage, ...] = ()


# The tools whose calls make calls of their own: a codemode script and a subagent.
# Each records them in its result, and neither may be called from inside the other.
_NESTED_CALL_HOSTS = frozenset({CODEMODE, SPAWN_AGENT})


@dataclass(slots=True)
class _ScriptCallLog:
    """The calls one codemode script or subagent makes, in the order it made them.

    The first `MAX_NESTED_CALLS` calls are recorded and later ones only counted, so
    a result stays bounded however many calls its limits allow. When the script or
    subagent ends the log closes: a call still unfinished is recorded then, as one
    that was ended before it finished, and nothing a call does later is recorded
    or published. Closing waits for nothing, so the result never depends on how
    fast a cancelled tool stops.

    A subagent's model responses are recorded here too, so its result accounts for
    them however the subagent ended.
    """

    parent_call_id: str
    # What an unfinished call's record says when its script or subagent ends first.
    ended_first: str = _SCRIPT_ENDED_FIRST
    omitted: int = 0
    closed: bool = False
    model_usage: list[ModelResponseUsage] = field(default_factory=list)
    _count: int = 0
    _recorded: dict[int, NestedToolCall] = field(default_factory=dict)
    _unfinished: dict[int, tuple[NestedToolCall, float]] = field(default_factory=dict)

    def start(self, unfinished: NestedToolCall) -> int | None:
        """Number the next call, given its record should the script end first.

        Returns:
            int | None: The call's place in the log, or None when it is past the
            cap and only counted.
        """

        index = self._count
        self._count += 1
        if index >= MAX_NESTED_CALLS:
            self.omitted += 1
            return None
        self._unfinished[index] = (unfinished, time.monotonic())
        return index

    def record(self, index: int | None, call: NestedToolCall) -> bool:
        """Record a call's outcome, unless the script has ended.

        Returns:
            bool: Whether the script is still running, so the call may be published.
        """

        if self.closed:
            return False
        if index is not None:
            self._unfinished.pop(index, None)
            self._recorded[index] = call
        return True

    def close(self) -> None:
        """End the script: record its unfinished calls and ignore anything later."""

        self.closed = True
        for index, (call, started) in self._unfinished.items():
            duration_ms = round((time.monotonic() - started) * 1000)
            self._recorded[index] = call.model_copy(update={"duration_ms": duration_ms})
        self._unfinished.clear()

    def calls(self) -> tuple[NestedToolCall, ...]:
        return tuple(self._recorded[index] for index in sorted(self._recorded))


# The script a codemode call is running. The call sets it around the script, and the
# script's host calls, which run in tasks started inside it, read it from there.
_SCRIPT_CALLS: ContextVar[_ScriptCallLog | None] = ContextVar("wisp_script_calls", default=None)


@dataclass(frozen=True)
class _RawToolResultSnapshot:
    """Extension-owned result fields copied without Wisp normalization."""

    text: object
    data: dict[str, object]
    truncated: object
    images: object


@dataclass(frozen=True)
class _ToolResultSnapshot:
    """Validated and bounded tool result used by Wisp normalization."""

    text: str
    data: dict[str, object]
    truncated: bool
    images: tuple[bytes, ...]


class _MalformedToolResultError(TypeError):
    """Raised when copied extension result fields violate the ToolResult contract."""


class BackgroundRequests:
    """The running foreground ``bash`` calls a user may move to the background, by call id.

    The executor registers a call while it runs and releases it when it ends; a
    request for any other call is refused.
    """

    def __init__(self) -> None:
        self._running: dict[str, asyncio.Event] = {}

    def register(self, call_id: str) -> asyncio.Event:
        """Track a foreground call; return the event that moves it."""

        event = asyncio.Event()
        self._running[call_id] = event
        return event

    def release(self, call_id: str) -> None:
        """Stop tracking a call that has ended."""

        self._running.pop(call_id, None)

    def request(self, call_id: str) -> bool:
        """Ask to move a running foreground call to the background.

        Args:
            call_id (str): The call to move.

        Returns:
            bool: Whether such a call is running; the call itself then ends with its
            process id, unless the command finishes first.
        """

        event = self._running.get(call_id)
        if event is None:
            return False
        event.set()
        return True


def _runs_in_foreground(tool_name: str, arguments: dict[str, object]) -> bool:
    """Whether this call runs a ``bash`` command to completion in the foreground."""

    return tool_name == "bash" and arguments.get("operation", "run") == "run"


class ConfiguredToolExecutor:
    """Adapt Wisp's registry and approval policies to the pure loop contract."""

    def __init__(
        self,
        *,
        registry: ToolRegistry | None,
        context: ToolContext,
        policy: ToolPolicy,
        approval_policy: ToolApprovalPolicy,
        progress_sink: LiveEventSink | None = None,
        mcp_catalog: McpCatalog | None = None,
        image_store: ImageStore | None = None,
        background_requests: BackgroundRequests | None = None,
    ) -> None:
        self._registry = registry
        # Where a running foreground `bash` call can be asked to move to the background.
        self._background_requests = background_requests
        self._context = context
        self._policy = policy
        self._approval_policy = approval_policy
        self._progress_sink = progress_sink
        self._mcp_catalog = mcp_catalog
        # Stores a tool's images before its result references them; without one a
        # result's images are dropped and the model is told so.
        self._image_store = image_store
        # Nested calls a script makes concurrently take turns through this, unless
        # their tool is parallel-safe; those run at most as many at once as in a batch.
        self._nested_serial_lock = anyio.Lock()
        self._nested_parallel_limit = anyio.CapacityLimiter(_MAX_PARALLEL_NESTED_CALLS)
        # What each `spawn_agent` call's subagents spent, by call id, until the
        # session has saved the call's result. A call that is cancelled never
        # returns one from here, and one that returned can still lose it when the
        # run is abandoned before it is published, so the spend is kept apart.
        self._unsaved_model_usage: dict[str, tuple[ModelResponseUsage, ...]] = {}

    @property
    def mcp_catalog(self) -> McpCatalog | None:
        """The MCP tools `mcp_call` may name this run, or None when exposure is direct."""

        return self._mcp_catalog

    def with_context(self, context: ToolContext) -> ConfiguredToolExecutor:
        """Return an executor for a subagent's calls, which run in ``context``.

        It shares this executor's registry, policy, approval policy, progress sink
        and MCP catalog, so a subagent's call passes exactly the checks a call of
        this executor's does. It shares the nested-call scheduling too: across
        subagents running at once, calls to tools that are not parallel-safe run one
        at a time and parallel-safe ones stay within one limit together. As within
        one script, the two kinds may overlap each other. It stores no images, as
        nested calls return only text.

        Args:
            context (ToolContext): The context the subagent's tools run with.

        Returns:
            ConfiguredToolExecutor: The executor to run the subagent's nested calls.
        """

        executor = ConfiguredToolExecutor(
            registry=self._registry,
            context=context,
            policy=self._policy,
            approval_policy=self._approval_policy,
            progress_sink=self._progress_sink,
            mcp_catalog=self._mcp_catalog,
        )
        executor._nested_serial_lock = self._nested_serial_lock
        executor._nested_parallel_limit = self._nested_parallel_limit
        return executor

    def unsaved_model_usage(self, call_id: str, *, forget: bool) -> tuple[ModelResponseUsage, ...]:
        """Return what a `spawn_agent` call's subagents spent, if no result has saved it.

        The session reads this for a result it is about to save that carries no
        usage of its own: one the loop made for a cancelled call, or one it
        synthesizes for a call whose result never arrived.

        Args:
            call_id (str): The call.
            forget (bool): Whether the call's result is now saved, with or without
                this. A provider may reuse the call id in a later round, which must
                not be charged these responses again.

        Returns:
            tuple[ModelResponseUsage, ...]: The responses, or none for a call that
            made no model request or whose result was already saved.
        """

        if forget:
            return self._unsaved_model_usage.pop(call_id, ())
        return self._unsaved_model_usage.get(call_id, ())

    @staticmethod
    def nested_host_call_id() -> str | None:
        """Return the id of the script or `spawn_agent` call this code runs inside.

        Returns:
            str | None: That call's id, or None outside such a call.
        """

        log = _SCRIPT_CALLS.get()
        return log.parent_call_id if log is not None else None

    @staticmethod
    def record_model_usage(usage: TokenUsage, cost: UsageCost | None) -> None:
        """Account one model response to the subagent call that is running.

        The response is added to that call's result, whether the subagent then
        finishes, fails or reaches its limit. Outside such a call, and once it has
        ended, the response is dropped.

        Args:
            usage (TokenUsage): The tokens the response reported.
            cost (UsageCost | None): Its list-price estimate, if the run has one.
        """

        log = _SCRIPT_CALLS.get()
        if log is not None and not log.closed:
            log.model_usage.append(ModelResponseUsage(usage=usage, cost=cost))

    def resolve_target(self, tool_call: ToolCall) -> str | None:
        """Return the MCP tool a proxied `mcp_call` names, or None; never raises.

        The loop calls this before announcing a call, so every event of a proxied
        call names the tool that will actually run.
        """

        if tool_call.name != MCP_CALL or tool_call.parse_error is not None:
            return None
        return resolve_target(self._mcp_catalog, tool_call.arguments)

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        prepared: PreparedToolExecution | None = None
        async for event in self.prepare(tool_call):
            if isinstance(event, PreparedToolExecution):
                prepared = event
            else:
                yield event
        if prepared is None:  # pragma: no cover - guarded by the implementation below
            raise RuntimeError("Tool preparation completed without an execution")
        yield await prepared.run()

    async def prepare(self, tool_call: ToolCall) -> AsyncIterator[ToolPreparationEvent]:
        """Resolve policy and approval while deferring all tool side effects."""

        outcome: _ToolRunOutcome
        checked = self._check_call(tool_call)
        if isinstance(checked, _ToolRunOutcome):
            outcome = checked
        else:
            tool, arguments, target = checked.tool, checked.arguments, checked.target
            if self._approval_policy.requires_approval(tool) and not self._approval_policy.approves(
                tool
            ):
                # The approval shows the call exactly as the model made it; for a
                # proxied call that names the target, so the user sees what runs.
                approval_arguments = dict(tool_call.arguments)
                self._approval_policy.prepare_approval(
                    tool,
                    call_id=tool_call.call_id,
                    arguments=approval_arguments,
                )
                approval_resolved = False
                try:
                    yield ToolApprovalRequested(
                        call_id=tool_call.call_id,
                        name=tool_call.name,
                        target=target,
                        arguments=approval_arguments,
                        safety=tool.safety,
                        grants_allowed=not confirms_every_call(tool),
                        session_only_yolo=self._approval_policy.session_only_yolo,
                    )
                    decision = await self._approval_policy.await_approval(
                        tool,
                        call_id=tool_call.call_id,
                        arguments=approval_arguments,
                    )
                    approval_resolved = True
                    yield ToolApprovalResolved(
                        call_id=tool_call.call_id,
                        name=tool_call.name,
                        target=target,
                        approved=decision.approved,
                        reason=decision.reason,
                    )
                finally:
                    if not approval_resolved:
                        self._approval_policy.cancel_approval(
                            call_id=tool_call.call_id,
                            reason="Agent run cancelled",
                        )
                if decision.approved:
                    yield self._prepare_tool(tool_call, tool, arguments, target=target)
                    return
                outcome = _failure_outcome(
                    decision.reason or "Tool execution was not approved",
                    failure_code="approval_denied",
                )
            else:
                yield self._prepare_tool(tool_call, tool, arguments, target=target)
                return

        yield self._prepare_outcome(tool_call, outcome, target=self.resolve_target(tool_call))

    def _check_call(self, tool_call: ToolCall) -> _CheckedCall | _ToolRunOutcome:
        """Find the tool a call runs and run every check that comes before approval.

        A proxied `mcp_call` continues as the MCP tool it names: from here on every
        check, the approval and the run see that tool, not the proxy. A rejected
        call fails before the user is asked, so a bad proposal never reaches an
        approval panel.
        """

        resolved = self._resolve_call(tool_call)
        if isinstance(resolved, _ToolRunOutcome):
            return resolved
        target, arguments = resolved.target, resolved.arguments
        try:
            tool = resolved.registry.get(target or tool_call.name)
        except UnknownToolError as exc:
            return _failure_outcome(str(exc), failure_code="unknown_tool")
        problem = tool.check_arguments(arguments) if isinstance(tool, ArgumentCheckedTool) else None
        if not self._policy.allows(tool):
            return _failure_outcome(self._policy.block_reason(tool), failure_code="policy_denied")
        if problem is not None:
            return _failure_outcome(problem, failure_code="invalid_arguments", retryable=True)
        return _CheckedCall(tool, arguments, target)

    async def run_nested(self, tool_call: ToolCall) -> NestedCallResult:
        """Run a call a codemode script made, under the same policy as a direct call.

        The call passes every check a direct call does, and the tool that runs must
        be read-only. It never asks the user: a call that would need approval fails
        unless the approval policy already grants it, so approving the script grants
        nothing it calls. A tool that is
        not parallel-safe waits for the other such nested calls, as it would in a
        batch. Nested calls report no progress of their own.

        Every call is recorded in its script's result, refused ones included, and
        published live as `tool.nested.started` and `tool.nested.ended`, outside
        the waits above. Publishing waits for the frontend, so a slow one slows the
        script; one that fails is logged and the call goes on. A call the script
        ended before it finished is recorded as such and publishes no end.

        Args:
            tool_call (ToolCall): The call, with an id unique within the script.

        Returns:
            NestedCallResult: The tool's text, and whether it failed.

        Raises:
            anyio.get_cancelled_exc_class(): When the script ends before the call
                finishes.
        """

        log = _SCRIPT_CALLS.get()
        if log is None:
            outcome = await self._nested_outcome(tool_call)
            return NestedCallResult(text=outcome.output, is_error=outcome.is_error)
        target = self.resolve_target(tool_call)
        arguments = _recorded_arguments(tool_call.arguments)
        started_at = utc_now()
        started = time.monotonic()

        def recorded(outcome: _ToolRunOutcome) -> NestedToolCall:
            summary = None if outcome.is_error else outcome.summary
            return NestedToolCall(
                call_id=tool_call.call_id,
                name=tool_call.name,
                target=target,
                arguments=arguments,
                is_error=outcome.is_error,
                failure_code=outcome.failure_code,
                error=outcome.output[:NESTED_CALL_TEXT_MAX_CHARS] if outcome.is_error else None,
                summary=summary[:NESTED_CALL_TEXT_MAX_CHARS] if summary is not None else None,
                started_at=started_at,
                duration_ms=round((time.monotonic() - started) * 1000),
            )

        ended_first = _ToolRunOutcome(output=log.ended_first, is_error=True)
        index = log.start(recorded(ended_first))
        try:
            await self._publish_nested(
                log,
                ToolNestedCallStarted(
                    parent_call_id=log.parent_call_id,
                    call_id=tool_call.call_id,
                    name=tool_call.name,
                    target=target,
                    arguments=arguments,
                ),
            )
            outcome = await self._nested_outcome(tool_call)
        except anyio.get_cancelled_exc_class():
            # Recorded without waiting: nothing may be published once the script ended.
            log.record(index, recorded(ended_first))
            raise
        call = recorded(outcome)
        if log.record(index, call):
            await self._publish_nested(
                log, ToolNestedCallEnded(parent_call_id=log.parent_call_id, call=call)
            )
        return NestedCallResult(text=outcome.output, is_error=outcome.is_error)

    async def _nested_outcome(self, tool_call: ToolCall) -> _ToolRunOutcome:
        """Check and run one nested call, or explain why it may not run."""

        if tool_call.name == CODEMODE:
            return _failure_outcome(
                "A script or a subagent cannot run a script.", failure_code="policy_denied"
            )
        if tool_call.name == SPAWN_AGENT:
            return _failure_outcome(
                "A script or a subagent cannot spawn a subagent.", failure_code="policy_denied"
            )
        checked = self._check_call(tool_call)
        if isinstance(checked, _ToolRunOutcome):
            return replace(checked, is_error=True)
        tool, arguments, target = checked.tool, checked.arguments, checked.target
        if tool.safety != "read":
            # Checked on the tool that runs, so no route, such as `mcp_call` naming a
            # command tool, reaches more than a read-only tool even under `--yes`.
            return _failure_outcome(
                f"{tool.name} is not read-only; a script or a subagent can call only "
                "read-only tools.",
                failure_code="policy_denied",
            )
        if self._approval_policy.requires_approval(tool) and not self._approval_policy.approves(
            tool
        ):
            return _failure_outcome(
                f"{tool.name} needs approval, which a script or a subagent cannot ask for. "
                "Call it directly instead.",
                failure_code="approval_denied",
            )
        name = target or tool_call.name
        parallel_safe = (
            self._registry.execution_metadata_for(name).parallel_safe
            if self._registry is not None
            else False
        )
        async with self._nested_parallel_limit if parallel_safe else self._nested_serial_lock:
            return await self._run_tool(
                tool, arguments, call_id=tool_call.call_id, tool_name=name, nested=True
            )

    async def _publish_nested(self, log: _ScriptCallLog, event: WispEvent) -> None:
        """Publish a nested call's event live; best effort, never once its script ended."""

        if self._progress_sink is None or log.closed:
            return
        try:
            await self._progress_sink(event)
        except Exception:  # noqa: BLE001 - the script's calls must not depend on a frontend
            _logger.exception("Could not publish a nested call's %s event", event.type)

    def _resolve_call(self, tool_call: ToolCall) -> _ResolvedCall | _ToolRunOutcome:
        """Find what a call will run, or the failure decided before any check.

        A proxied `mcp_call` resolves to the catalog tool it names, with that tool's
        own arguments; every other call resolves to itself.
        """

        if tool_call.parse_error is not None:
            return _failure_outcome(
                tool_call.parse_error,
                failure_code="invalid_arguments",
                retryable=True,
                recovery_hint="Retry with arguments that match the tool's input schema.",
            )
        if self._registry is None:
            return _failure_outcome(
                "Tool execution is not configured",
                failure_code="internal_error",
            )
        arguments = dict(tool_call.arguments)
        catalog = self._mcp_catalog
        if tool_call.name != MCP_CALL or catalog is None:
            return _ResolvedCall(self._registry, arguments)
        name = arguments.get("name")
        if not isinstance(name, str):
            return _failure_outcome(
                f"{MCP_CALL}.name must be a string",
                failure_code="invalid_arguments",
                retryable=True,
            )
        entry = catalog.get(name)
        if entry is None:
            registered = self._registry is not None and name in self._registry.names()
            return _failure_outcome(
                f"{name} is not an MCP tool available to {MCP_CALL} in this run"
                if registered
                else f"No MCP tool named {name} is available in this run",
                failure_code="policy_denied" if registered else "unknown_tool",
                retryable=not registered,
                recovery_hint=None if registered else "Find the tool's name with mcp_search.",
            )
        tool_arguments = arguments.get("arguments")
        problem = check_proxied_arguments(entry, tool_arguments)
        if problem is not None:
            return _failure_outcome(problem, failure_code="invalid_arguments", retryable=True)
        return _ResolvedCall(
            self._registry, dict(cast(Mapping[str, object], tool_arguments)), target=name
        )

    def _prepare_tool(
        self,
        tool_call: ToolCall,
        tool: Tool,
        arguments: dict[str, object],
        *,
        target: str | None = None,
    ) -> PreparedToolExecution:
        async def run() -> ToolExecutionEnded:
            if tool_call.name not in _NESTED_CALL_HOSTS:
                outcome = await self._run_tool(
                    tool,
                    arguments,
                    call_id=tool_call.call_id,
                    tool_name=target or tool_call.name,
                )
                return _execution_ended(tool_call, outcome, target=target)
            log = _ScriptCallLog(
                tool_call.call_id,
                ended_first=(
                    _SUBAGENT_ENDED_FIRST if tool_call.name == SPAWN_AGENT else _SCRIPT_ENDED_FIRST
                ),
            )
            token = _SCRIPT_CALLS.set(log)
            try:
                outcome = await self._run_tool(
                    tool, arguments, call_id=tool_call.call_id, tool_name=tool_call.name
                )
            finally:
                # Closed before anything reads it, on every path: a cancelled call
                # that stops only later can neither change the result nor publish
                # after it.
                log.close()
                _SCRIPT_CALLS.reset(token)
                if log.model_usage:
                    self._unsaved_model_usage[tool_call.call_id] = tuple(log.model_usage)
            outcome = replace(
                outcome,
                nested_calls=log.calls(),
                nested_calls_omitted=log.omitted,
                model_usage=tuple(log.model_usage),
            )
            return _execution_ended(tool_call, outcome, target=target)

        # Scheduling follows the tool that runs: a proxied call is as parallel-safe
        # as its target, never as the proxy.
        parallel_safe = (
            self._registry.execution_metadata_for(target or tool_call.name).parallel_safe
            if self._registry is not None
            else False
        )
        return PreparedToolExecution(
            call_id=tool_call.call_id,
            name=tool_call.name,
            parallel_safe=parallel_safe,
            runner=run,
        )

    @staticmethod
    def _prepare_outcome(
        tool_call: ToolCall,
        outcome: _ToolRunOutcome,
        *,
        target: str | None = None,
    ) -> PreparedToolExecution:
        async def run() -> ToolExecutionEnded:
            return _execution_ended(tool_call, outcome, target=target)

        # The runner only publishes an outcome decided during preparation and never
        # reaches the tool, so a rejected call must not force its batch to run serially.
        return PreparedToolExecution(
            call_id=tool_call.call_id,
            name=tool_call.name,
            parallel_safe=True,
            runner=run,
        )

    async def _run_tool(
        self,
        tool: Tool,
        arguments: dict[str, object],
        *,
        call_id: str,
        tool_name: str,
        nested: bool = False,
    ) -> _ToolRunOutcome:
        """Run ``tool`` and normalize its result.

        A ``nested`` call, one a codemode script or a subagent made, reports no
        progress of its own and stores no images, since only its text reaches the
        script or subagent; a result that had an image says it was left out.
        """

        try:
            if not nested:
                result = await self._run_reporting_progress(
                    tool, arguments, call_id=call_id, tool_name=tool_name
                )
            else:
                result = await tool.run(arguments, self._context)
        except Exception as exc:  # noqa: BLE001 - tool failures are recoverable results
            return _tool_exception_outcome(exc)

        # Access and copy extension-owned fields in isolation. A malformed result must
        # remain an ordinary recoverable tool error.
        try:
            raw_snapshot = _read_tool_result(result, tool_name=tool_name)
        except Exception:  # noqa: BLE001 - malformed extension result
            return _failure_outcome(
                "Tool returned an invalid result", failure_code="invalid_result"
            )

        try:
            snapshot = _normalize_tool_result(
                raw_snapshot, tool_name=tool_name, context=self._context
            )
            outcome = _ToolRunOutcome(
                output=snapshot.text,
                exit_code=_promote_exit_code(tool_name, snapshot.data),
                output_has_exit_status=_promote_output_has_exit_status(
                    tool_name,
                    snapshot.data,
                ),
                before_text=_promote_before_text(tool_name, snapshot.data),
                created=_promote_created(tool_name, snapshot.data),
                edit_locations=_promote_edit_locations(tool_name, snapshot.data),
                summary=summarize_tool_result(
                    tool_name,
                    snapshot.data,
                    truncated=_promote_truncated(snapshot.truncated),
                ),
                # The tool's own authoritative "I capped my output" flag, so the card
                # can be honest that an expanded view may still not be the whole story.
                # Only a real ToolResult sets this; every synthetic/error path defaults
                # it False.
                truncated=_promote_truncated(snapshot.truncated),
                process_id=_promote_process_id(tool_name, snapshot.data),
                process_state=_promote_process_state(tool_name, snapshot.data),
                process_error=_promote_process_error(tool_name, snapshot.data),
                stdout=_promote_process_output(tool_name, snapshot.data, "stdout"),
                stderr=_promote_process_output(tool_name, snapshot.data, "stderr"),
                stdout_truncated=_promote_output_truncated(
                    tool_name,
                    snapshot.data,
                    "stdout_truncated",
                ),
                stderr_truncated=_promote_output_truncated(
                    tool_name,
                    snapshot.data,
                    "stderr_truncated",
                ),
                stdout_dropped_bytes=_promote_dropped_bytes(
                    tool_name,
                    snapshot.data,
                    "stdout_dropped_bytes",
                ),
                stderr_dropped_bytes=_promote_dropped_bytes(
                    tool_name,
                    snapshot.data,
                    "stderr_dropped_bytes",
                ),
                process_started_at=_promote_process_time(
                    tool_name, snapshot.data, "process_started_at"
                ),
                process_ended_at=_promote_process_time(
                    tool_name, snapshot.data, "process_ended_at"
                ),
            )
        except _MalformedToolResultError:
            return _failure_outcome(
                "Tool returned an invalid result", failure_code="invalid_result"
            )
        except Exception as exc:
            raise ToolResultProcessingError(call_id=call_id, tool_name=tool_name) from exc
        if nested:
            if not snapshot.images:
                return outcome
            return replace(outcome, output=f"{outcome.output}\n{_IMAGE_NOT_ATTACHED}")
        return await self._with_stored_images(outcome, snapshot.images)

    async def _with_stored_images(
        self, outcome: _ToolRunOutcome, images: tuple[bytes, ...]
    ) -> _ToolRunOutcome:
        """Store a result's images and attach their references to the outcome.

        Storing comes before the result is published, so every reference a tool
        result carries already resolves in the session's image store.

        Args:
            outcome (_ToolRunOutcome): The normalized result without images.
            images (tuple[bytes, ...]): The image files the tool returned.

        Returns:
            _ToolRunOutcome: The outcome with its image references, or a failure
            when an image is invalid or cannot be stored.
        """

        if not images:
            return outcome
        if self._image_store is None:
            return replace(
                outcome,
                output=f"{outcome.output}\n{_IMAGE_NOT_ATTACHED}",
            )
        try:
            stored = tuple([await self._image_store(image) for image in images])
        except ImageRejectedError as exc:
            return _failure_outcome(
                f"Tool returned an image Wisp cannot use: {exc}", failure_code="invalid_result"
            )
        except SessionError:
            _logger.exception("Could not store a tool result's image")
            return _failure_outcome(
                "Could not store the image the tool returned", failure_code="internal_error"
            )
        return replace(outcome, images=stored)

    async def _run_reporting_progress(
        self,
        tool: Tool,
        arguments: dict[str, object],
        *,
        call_id: str,
        tool_name: str,
    ) -> ToolResult:
        """Run a tool, giving it a progress handle when a frontend is listening.

        A foreground ``bash`` call also gets the event that moves it to the
        background, while it runs.
        """

        context = self._context
        requests = self._background_requests
        movable = requests is not None and _runs_in_foreground(tool_name, arguments)
        if requests is not None and movable:
            context = replace(context, background_request=requests.register(call_id))
        try:
            sink = self._progress_sink
            if sink is None:
                return await tool.run(arguments, context)

            async def run(progress: ToolProgress) -> ToolResult:
                return await tool.run(arguments, replace(context, progress=progress))

            return await run_with_progress(sink, call_id=call_id, name=tool_name, run=run)
        finally:
            if requests is not None and movable:
                requests.release(call_id)


@dataclass(frozen=True, slots=True)
class _CheckedCall:
    """A call that passed every check before approval: its tool and what it runs as."""

    tool: Tool
    arguments: dict[str, object]
    target: str | None = None


@dataclass(frozen=True, slots=True)
class _ResolvedCall:
    """What one call will run: its registry, arguments, and a proxy's target."""

    registry: ToolRegistry
    arguments: dict[str, object]
    target: str | None = None


def _execution_ended(
    tool_call: ToolCall, outcome: _ToolRunOutcome, *, target: str | None = None
) -> ToolExecutionEnded:
    return ToolExecutionEnded(
        call_id=tool_call.call_id,
        name=tool_call.name,
        target=target,
        output=outcome.output,
        is_error=outcome.is_error,
        failure_code=outcome.failure_code,
        retryable=outcome.retryable,
        recovery_hint=outcome.recovery_hint,
        exit_code=outcome.exit_code,
        output_has_exit_status=outcome.output_has_exit_status,
        before_text=outcome.before_text,
        created=outcome.created,
        edit_locations=outcome.edit_locations,
        summary=outcome.summary,
        truncated=outcome.truncated,
        process_id=outcome.process_id,
        process_state=outcome.process_state,
        process_error=outcome.process_error,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
        stdout_truncated=outcome.stdout_truncated,
        stderr_truncated=outcome.stderr_truncated,
        stdout_dropped_bytes=outcome.stdout_dropped_bytes,
        stderr_dropped_bytes=outcome.stderr_dropped_bytes,
        process_started_at=outcome.process_started_at,
        process_ended_at=outcome.process_ended_at,
        images=outcome.images,
        nested_calls=outcome.nested_calls,
        nested_calls_omitted=outcome.nested_calls_omitted,
        model_usage=outcome.model_usage,
    )


def _recorded_arguments(arguments: Mapping[str, object]) -> JsonObject | None:
    """A nested call's arguments as its record keeps them, or None when too large."""

    try:
        text = json.dumps(arguments, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None
    if len(text) > NESTED_CALL_ARGUMENTS_MAX_CHARS:
        return None
    decoded = json.loads(text)
    return cast(JsonObject, decoded) if isinstance(decoded, dict) else None


def _read_tool_result(result: ToolResult, *, tool_name: str) -> _RawToolResultSnapshot:
    """Read only extension-owned fields and recognized metadata keys."""

    text = result.text
    data = result.data
    truncated = result.truncated
    if not isinstance(data, Mapping):
        raise TypeError("ToolResult.data must be a mapping")
    raw_data = {key: data.get(key) for key in _RESULT_DATA_KEYS.get(tool_name, ())}
    return _RawToolResultSnapshot(
        text=text, data=raw_data, truncated=truncated, images=result.images
    )


def _normalize_tool_result(
    result: _RawToolResultSnapshot,
    *,
    tool_name: str,
    context: ToolContext,
) -> _ToolResultSnapshot:
    """Validate and bound copied fields using only Wisp-owned operations."""

    text = result.text
    if type(text) is not str:
        raise _MalformedToolResultError("ToolResult.text must be a string")
    _require_utf8(text, field="ToolResult.text")
    truncated = result.truncated
    data = _snapshot_result_data(tool_name, result.data, context=context)
    has_exit_status = _promote_output_has_exit_status(tool_name, data)
    status_overhead = len("Command exited with code -2147483648: ") if has_exit_status else 0
    managed_text_overhead = _bash_managed_process_text_overhead(tool_name, data)
    metadata_overhead = max(status_overhead, managed_text_overhead)
    metadata_line_overhead = max(
        1 if has_exit_status else 0,
        _bash_managed_process_line_overhead(tool_name, data),
    )
    preserves_status_line = metadata_overhead > 0
    bounded_text = truncate_text(
        text,
        # Bash fixed status lines and stream labels are metadata outside the
        # body budget. Allow their bounded worst-case size through normalization
        # so a tiny embedding budget cannot erase the exit code, managed process
        # id, or the retained output line that follows a label.
        max_bytes=max(0, context.max_output_bytes) + metadata_overhead,
        max_lines=max(0, context.max_output_lines) + metadata_line_overhead
        if preserves_status_line
        else max(0, context.max_output_lines),
    )
    images = result.images
    if (
        type(images) is not tuple
        or len(images) > MAX_TOOL_RESULT_IMAGES
        or any(type(image) is not bytes for image in images)
    ):
        raise _MalformedToolResultError(
            f"ToolResult.images must be a tuple of at most {MAX_TOOL_RESULT_IMAGES} bytes objects"
        )
    return _ToolResultSnapshot(
        text=bounded_text.text,
        data=data,
        truncated=(truncated if type(truncated) is bool else False) or bounded_text.truncated,
        images=cast(tuple[bytes, ...], images),
    )


def _snapshot_result_data(
    tool_name: str,
    data: Mapping[str, object],
    *,
    context: ToolContext,
) -> dict[str, object]:
    """Extract the small primitive metadata surface Wisp currently consumes."""

    snapshot: dict[str, object] = {}
    if tool_name == "bash":
        _copy_bounded_int(
            data,
            snapshot,
            "exit_code",
            minimum=_MIN_EXIT_CODE,
            maximum=_MAX_EXIT_CODE,
        )
        _copy_exact(data, snapshot, "output_has_exit_status", bool)
        _copy_bash_process_data(data, snapshot, context=context)
    elif tool_name == "write":
        before_text = data.get("before_text")
        if type(before_text) is str:
            _require_utf8(before_text, field="ToolResult.data['before_text']")
            bounded_before = truncate_text(
                before_text,
                max_bytes=max(0, context.max_output_bytes),
                max_lines=max(0, context.max_output_lines),
            )
            if not bounded_before.truncated:
                snapshot["before_text"] = bounded_before.text
        _copy_exact(data, snapshot, "created", bool)
    elif tool_name == "edit":
        locations = _edit_locations(data.get("locations"))
        if locations:
            snapshot["locations"] = locations
    elif tool_name == "read":
        _copy_result_count(data, snapshot, "line_count")
        _copy_result_count(data, snapshot, "selected_count")
        _copy_exact(data, snapshot, "path", str)
    elif tool_name in {"grep", "find"}:
        _copy_result_count(data, snapshot, "count")
    elif tool_name == "ls":
        _copy_result_count(data, snapshot, "entry_count")
        if "entry_count" not in snapshot:
            entries = data.get("entries")
            if type(entries) is list:
                entry_count = len(entries)
                if entry_count <= _MAX_RESULT_COUNT:
                    snapshot["entry_count"] = entry_count
        _copy_exact(data, snapshot, "path", str)
    elif tool_name == CODEMODE:
        _copy_result_count(data, snapshot, "calls")
        _copy_result_count(data, snapshot, "failed_calls")
    return snapshot


def _copy_bash_process_data(
    source: Mapping[str, object],
    target: dict[str, object],
    *,
    context: ToolContext,
) -> None:
    _copy_bounded_scalar_text(
        source,
        target,
        "process_id",
        max_bytes=_MAX_PROCESS_ID_BYTES,
    )
    state = source.get("process_state")
    if type(state) is str and state in _PROCESS_STATES:
        target["process_state"] = state
    _copy_bounded_metadata_text(
        source,
        target,
        "process_error",
        max_bytes=_MAX_PROCESS_ERROR_BYTES,
    )
    _copy_exact(source, target, "stdout_truncated", bool)
    _copy_exact(source, target, "stderr_truncated", bool)
    if _copy_bounded_output_text(source, target, "stdout", context=context):
        target["stdout_truncated"] = True
    if _copy_bounded_output_text(source, target, "stderr", context=context):
        target["stderr_truncated"] = True
    _copy_result_count(source, target, "stdout_dropped_bytes")
    _copy_result_count(source, target, "stderr_dropped_bytes")
    for key in ("process_started_at", "process_ended_at"):
        value = source.get(key)
        # Only timezone-aware times are comparable with the frontend's clock.
        if type(value) is datetime and value.tzinfo is not None:
            target[key] = value


def _bash_managed_process_text_overhead(
    tool_name: str,
    data: Mapping[str, object],
) -> int:
    process_id = _promote_process_id(tool_name, data)
    state = _promote_process_state(tool_name, data)
    if process_id is None or state is None:
        return 0

    if state == "running":
        header = f"Process {process_id} is still running"
    elif state == "completed":
        header = f"Process {process_id} completed with exit code {_MIN_EXIT_CODE}"
    elif state == "timed_out":
        header = f"Process {process_id} timed out"
    elif state == "cancelled":
        header = f"Process {process_id} cancelled"
    else:
        error = _promote_process_error(tool_name, data)
        header = (
            f"Process {process_id} failed: {error}" if error else f"Process {process_id} failed: "
        )
    return (
        len(header.encode("utf-8"))
        + _bash_managed_stream_label_byte_overhead(data)
        + _TRUNCATION_SUFFIX_WITH_SEPARATOR_BYTES
    )


def _copy_bounded_metadata_text(
    source: Mapping[str, object],
    target: dict[str, object],
    key: str,
    *,
    max_bytes: int,
) -> bool:
    value = source.get(key)
    if type(value) is not str:
        return False
    _require_utf8(value, field=f"ToolResult.data[{key!r}]")
    lines = value.splitlines()
    first_line = lines[0] if lines else value
    bounded = truncate_text(first_line, max_bytes=max_bytes, max_lines=1)
    target[key] = bounded.text
    return bounded.truncated or first_line != value


def _bash_managed_process_line_overhead(
    tool_name: str,
    data: Mapping[str, object],
) -> int:
    if _promote_process_id(tool_name, data) is None:
        return 0
    if _promote_process_state(tool_name, data) is None:
        return 0
    return 1 + _bash_managed_stream_count(data)


def _bash_managed_stream_label_byte_overhead(data: Mapping[str, object]) -> int:
    has_stdout = bool(_promote_process_output("bash", data, "stdout"))
    has_stderr = bool(_promote_process_output("bash", data, "stderr"))
    byte_overhead = 0
    if has_stdout:
        byte_overhead += len(b"stdout:\n")
    if has_stderr:
        byte_overhead += len(b"stderr:\n")
    if has_stdout and has_stderr:
        byte_overhead += len(b"\n")
    return byte_overhead


def _bash_managed_stream_count(data: Mapping[str, object]) -> int:
    return int(bool(_promote_process_output("bash", data, "stdout"))) + int(
        bool(_promote_process_output("bash", data, "stderr"))
    )


def _copy_bounded_scalar_text(
    source: Mapping[str, object],
    target: dict[str, object],
    key: str,
    *,
    max_bytes: int,
) -> None:
    value = source.get(key)
    if type(value) is not str:
        return
    _require_utf8(value, field=f"ToolResult.data[{key!r}]")
    if "\n" in value or "\r" in value:
        return
    bounded = truncate_text(value, max_bytes=max_bytes, max_lines=1)
    if not bounded.truncated:
        target[key] = bounded.text


def _copy_bounded_output_text(
    source: Mapping[str, object],
    target: dict[str, object],
    key: str,
    *,
    context: ToolContext,
) -> bool:
    value = source.get(key)
    if type(value) is not str:
        return False
    _require_utf8(value, field=f"ToolResult.data[{key!r}]")
    bounded = truncate_text(
        value,
        max_bytes=max(0, context.max_output_bytes),
        max_lines=max(0, context.max_output_lines),
    )
    target[key] = bounded.text
    return bounded.truncated


def _copy_exact(
    source: Mapping[str, object],
    target: dict[str, object],
    key: str,
    expected_type: type[object],
) -> None:
    value = source.get(key)
    if type(value) is expected_type:
        if expected_type is str:
            _require_utf8(cast(str, value), field=f"ToolResult.data[{key!r}]")
        target[key] = value


def _require_utf8(value: str, *, field: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _MalformedToolResultError(f"{field} must be valid UTF-8 text") from exc


def _copy_result_count(source: Mapping[str, object], target: dict[str, object], key: str) -> None:
    _copy_bounded_int(source, target, key, minimum=0, maximum=_MAX_RESULT_COUNT)


def _copy_bounded_int(
    source: Mapping[str, object],
    target: dict[str, object],
    key: str,
    *,
    minimum: int,
    maximum: int,
) -> None:
    value = source.get(key)
    if type(value) is int and minimum <= value <= maximum:
        target[key] = value


def _failure_outcome(
    message: str,
    *,
    failure_code: ToolFailureCode | None,
    retryable: bool = False,
    recovery_hint: str | None = None,
) -> _ToolRunOutcome:
    if recovery_hint is None:
        output = _truncate_failure_text(message, _MODEL_VISIBLE_TOOL_ERROR_MAX_CHARS)
    else:
        recovery = f"\nRecovery: {recovery_hint}"
        message_budget = _MODEL_VISIBLE_TOOL_ERROR_MAX_CHARS - len(recovery)
        output = f"{_truncate_failure_text(message, message_budget)}{recovery}"
    return _ToolRunOutcome(
        output=output,
        is_error=True,
        failure_code=failure_code,
        retryable=retryable,
        recovery_hint=recovery_hint,
    )


def _truncate_failure_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def _tool_exception_outcome(exc: Exception) -> _ToolRunOutcome:
    """Expose only validated metadata from explicitly model-safe tool errors."""

    if not isinstance(exc, ToolError):
        return _failure_outcome("Tool execution failed", failure_code="internal_error")
    try:
        message = str(exc)
        failure_code = exc.failure_code
        retryable = exc.retryable
        recovery_hint = exc.recovery_hint
        if not message:
            raise ValueError
        message.encode("utf-8")
        if failure_code is not None and failure_code not in _FAILURE_CODES:
            raise ValueError
        if type(retryable) is not bool:
            raise ValueError
        if failure_code is None and (retryable or recovery_hint is not None):
            raise ValueError
        if recovery_hint is not None:
            if type(recovery_hint) is not str or len(recovery_hint) > 500:
                raise ValueError
            recovery_hint.encode("utf-8")
    except Exception:  # noqa: BLE001 - defensive against hostile subclasses
        return _failure_outcome("Tool execution failed", failure_code="internal_error")
    return _failure_outcome(
        message,
        failure_code=failure_code,
        retryable=retryable,
        recovery_hint=recovery_hint,
    )


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


def _promote_output_has_exit_status(name: str, data: Mapping[str, object]) -> bool:
    """Whether Bash text carries Wisp's synthetic completion envelope."""

    if name not in _EXIT_CODE_TOOLS:
        return False
    return data.get("output_has_exit_status") is True


def _promote_process_id(name: str, data: Mapping[str, object]) -> str | None:
    if name != "bash":
        return None
    process_id = data.get("process_id")
    return process_id if isinstance(process_id, str) else None


def _promote_process_state(
    name: str,
    data: Mapping[str, object],
) -> ManagedProcessState | None:
    if name != "bash":
        return None
    state = data.get("process_state")
    return state if type(state) is str and state in _PROCESS_STATES else None


def _promote_process_error(name: str, data: Mapping[str, object]) -> str | None:
    if name != "bash":
        return None
    error = data.get("process_error")
    return error if isinstance(error, str) else None


def _promote_process_time(name: str, data: Mapping[str, object], key: str) -> datetime | None:
    if name != "bash":
        return None
    value = data.get(key)
    return value if isinstance(value, datetime) else None


def _promote_process_output(
    name: str,
    data: Mapping[str, object],
    key: str,
) -> str | None:
    if name != "bash":
        return None
    output = data.get(key)
    return output if isinstance(output, str) else None


def _promote_output_truncated(
    name: str,
    data: Mapping[str, object],
    key: str,
) -> bool:
    if name != "bash":
        return False
    return data.get(key) is True


def _promote_dropped_bytes(
    name: str,
    data: Mapping[str, object],
    key: str,
) -> int:
    if name != "bash":
        return 0
    value = data.get(key)
    return value if type(value) is int and 0 <= value <= _MAX_RESULT_COUNT else 0


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


def _edit_locations(value: object) -> tuple[EditLocation, ...]:
    """Validate an edit tool's reported locations; any malformed entry drops them all.

    A call with more than ``MAX_EDIT_LOCATIONS`` edits reports none, rather than
    numbering only some of its diff.
    """

    if type(value) is not list or not 0 < len(value) <= MAX_EDIT_LOCATIONS:
        return ()
    try:
        return tuple(EditLocation.model_validate(location, strict=True) for location in value)
    except ValidationError:
        return ()


def _promote_edit_locations(name: str, data: Mapping[str, object]) -> tuple[EditLocation, ...]:
    """Where each edit landed, for the built-in edit tool only.

    Gated by name, like ``before_text``, so another tool's ``locations`` cannot
    renumber a diff.
    """

    if name != "edit":
        return ()
    locations = data.get("locations")
    return locations if type(locations) is tuple else ()


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


def _promote_truncated(truncated: object) -> bool:
    """Coerce a tool's ``truncated`` flag to a strict bool for the event model.

    ``ToolResult`` is a plain dataclass with no validation, so a custom or malformed
    extension tool can hand back ``None`` or a non-bool. The event models type it as a
    strict ``bool``, and the ``ToolExecutionEnded`` that carries it is built outside
    ``_run_tool``'s try/except — so an un-coerced odd value would raise a Pydantic
    ``ValidationError`` there and abort the tool stream instead of degrading to a
    model-visible tool error. Gate it like the other promoted fields: only a real
    ``bool`` counts (not truthiness, so ``truncated="no"`` doesn't read as capped),
    everything else defaults to "not truncated".
    """

    return truncated if isinstance(truncated, bool) else False


__all__ = ["ConfiguredToolExecutor"]
