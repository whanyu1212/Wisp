"""Coding-layer tool registry, policy, and approval adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import cast

from wisp.agent.execution import ToolExecutionEvent, ToolResultProcessingError
from wisp.events import ToolApprovalRequested, ToolApprovalResolved, ToolExecutionEnded
from wisp.providers.events import ToolCall
from wisp.runtime.registry import ToolRegistry, UnknownToolError
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.base import Tool
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.summary import summarize_tool_result
from wisp.tools.truncation import truncate_text

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
_MAX_RESULT_COUNT = (1 << 63) - 1
_MIN_EXIT_CODE = -(1 << 31)
_MAX_EXIT_CODE = (1 << 31) - 1
_RESULT_DATA_KEYS = {
    "bash": ("exit_code", "output_has_exit_status"),
    "write": ("before_text", "created"),
    "read": ("line_count", "selected_count", "path"),
    "grep": ("count",),
    "find": ("count",),
    "ls": ("entries", "path"),
}


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
    output_has_exit_status: bool = False
    before_text: str | None = None
    created: bool = False
    summary: str | None = None
    truncated: bool = False


@dataclass(frozen=True)
class _RawToolResultSnapshot:
    """Extension-owned result fields copied without Wisp normalization."""

    text: object
    data: dict[str, object]
    truncated: object


@dataclass(frozen=True)
class _ToolResultSnapshot:
    """Validated and bounded tool result used by Wisp normalization."""

    text: str
    data: dict[str, object]
    truncated: bool


class _MalformedToolResultError(TypeError):
    """Raised when copied extension result fields violate the ToolResult contract."""


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
                        outcome = await self._run_tool(
                            tool,
                            arguments,
                            call_id=tool_call.call_id,
                            tool_name=tool_call.name,
                        )
                    else:
                        outcome = _ToolRunOutcome(
                            decision.reason or "Tool execution was not approved",
                            is_error=True,
                        )
                else:
                    outcome = await self._run_tool(
                        tool,
                        arguments,
                        call_id=tool_call.call_id,
                        tool_name=tool_call.name,
                    )

        yield ToolExecutionEnded(
            call_id=tool_call.call_id,
            name=tool_call.name,
            output=outcome.output,
            is_error=outcome.is_error,
            exit_code=outcome.exit_code,
            output_has_exit_status=outcome.output_has_exit_status,
            before_text=outcome.before_text,
            created=outcome.created,
            summary=outcome.summary,
            truncated=outcome.truncated,
        )

    async def _run_tool(
        self,
        tool: Tool,
        arguments: dict[str, object],
        *,
        call_id: str,
        tool_name: str,
    ) -> _ToolRunOutcome:
        try:
            result = await tool.run(arguments, self._context)
        except Exception as exc:  # noqa: BLE001 - tool failures are recoverable results
            return _ToolRunOutcome(_model_visible_tool_error(exc), is_error=True)

        # Access and copy extension-owned fields in isolation. A malformed result must
        # remain an ordinary recoverable tool error.
        try:
            raw_snapshot = _read_tool_result(result, tool_name=tool_name)
        except Exception:  # noqa: BLE001 - malformed extension result
            return _ToolRunOutcome("Tool returned an invalid result", is_error=True)

        try:
            snapshot = _normalize_tool_result(
                raw_snapshot, tool_name=tool_name, context=self._context
            )
            return _ToolRunOutcome(
                output=snapshot.text,
                exit_code=_promote_exit_code(tool_name, snapshot.data),
                output_has_exit_status=_promote_output_has_exit_status(
                    tool_name,
                    snapshot.data,
                ),
                before_text=_promote_before_text(tool_name, snapshot.data),
                created=_promote_created(tool_name, snapshot.data),
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
            )
        except _MalformedToolResultError:
            return _ToolRunOutcome("Tool returned an invalid result", is_error=True)
        except Exception as exc:
            raise ToolResultProcessingError(call_id=call_id, tool_name=tool_name) from exc


def _read_tool_result(result: ToolResult, *, tool_name: str) -> _RawToolResultSnapshot:
    """Read only extension-owned fields and recognized metadata keys."""

    text = result.text
    data = result.data
    truncated = result.truncated
    if not isinstance(data, Mapping):
        raise TypeError("ToolResult.data must be a mapping")
    raw_data = {key: data.get(key) for key in _RESULT_DATA_KEYS.get(tool_name, ())}
    return _RawToolResultSnapshot(text=text, data=raw_data, truncated=truncated)


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
    has_exit_status = _promote_output_has_exit_status(tool_name, result.data)
    status_overhead = len("Command exited with code -2147483648: ") if has_exit_status else 0
    bounded_text = truncate_text(
        text,
        # A Bash result's fixed completion envelope is metadata outside the body
        # budget. Allow its bounded worst-case size through normalization so a
        # tiny embedding budget cannot erase the exit code the tool preserved.
        max_bytes=max(0, context.max_output_bytes) + status_overhead,
        max_lines=max(1, context.max_output_lines)
        if has_exit_status
        else max(0, context.max_output_lines),
    )
    return _ToolResultSnapshot(
        text=bounded_text.text,
        data=_snapshot_result_data(tool_name, result.data, context=context),
        truncated=(truncated if type(truncated) is bool else False) or bounded_text.truncated,
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
    elif tool_name == "read":
        _copy_result_count(data, snapshot, "line_count")
        _copy_result_count(data, snapshot, "selected_count")
        _copy_exact(data, snapshot, "path", str)
    elif tool_name in {"grep", "find"}:
        _copy_result_count(data, snapshot, "count")
    elif tool_name == "ls":
        entries = data.get("entries")
        if type(entries) is list:
            entry_count = len(entries)
            if entry_count <= _MAX_RESULT_COUNT:
                snapshot["entry_count"] = entry_count
        _copy_exact(data, snapshot, "path", str)
    return snapshot


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


def _model_visible_tool_error(exc: Exception) -> str:
    """Return bounded tool-facing detail only for explicitly model-safe errors."""

    if not isinstance(exc, ToolError):
        return "Tool execution failed"
    try:
        message = str(exc)
    except Exception:  # pragma: no cover - defensive against hostile subclasses
        return "Tool execution failed"
    if not message:
        return "Tool execution failed"
    try:
        message.encode("utf-8")
    except UnicodeEncodeError:
        return "Tool execution failed"
    if len(message) <= _MODEL_VISIBLE_TOOL_ERROR_MAX_CHARS:
        return message
    return message[: _MODEL_VISIBLE_TOOL_ERROR_MAX_CHARS - 3] + "..."


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
