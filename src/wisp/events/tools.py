"""Tool call, approval, trust, and tool-result lifecycle events."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from wisp.events._base import (
    EndedProcessState,
    ImagePart,
    JsonObject,
    ManagedProcessState,
    WispEvent,
    _MessageOriginEvent,
)
from wisp.events.usage import TokenUsage, UsageCost
from wisp.tool_types import ToolFailureCode

# How much of a process's command a process.ended event carries.
PROCESS_COMMAND_SUMMARY_MAX_CHARS = 500


class ToolCallRequested(WispEvent):
    type: Literal["tool.call"] = "tool.call"
    call_id: str
    name: str
    arguments: JsonObject


class ToolExecutionStarted(WispEvent):
    type: Literal["tool.execution.started"] = "tool.execution.started"
    call_id: str
    name: str
    arguments: JsonObject
    # The MCP tool a proxied `mcp_call` runs; `name` stays the tool the model called.
    target: str | None = None


class ToolApprovalRequested(WispEvent):
    type: Literal["tool.approval.requested"] = "tool.approval.requested"
    call_id: str
    name: str
    # The MCP tool a proxied `mcp_call` runs, which is what the approval grants.
    target: str | None = None
    arguments: JsonObject
    safety: Literal["read", "mutating", "command"]
    # False for tools that need the user's confirmation on every call: the
    # frontend offers only a one-time confirmation or a denial, never a grant.
    grants_allowed: bool = True
    # True in the home folder or a folder above it: choosing YOLO applies for this
    # session and is not saved for the project.
    session_only_yolo: bool = False


class ToolApprovalResolved(WispEvent):
    type: Literal["tool.approval.resolved"] = "tool.approval.resolved"
    call_id: str
    name: str
    target: str | None = None
    approved: bool
    reason: str | None = None


# Bounds of one progress snapshot's output tail, per stream. The tail is already
# bounded by bytes where it is sampled; characters never outnumber UTF-8 bytes.
TOOL_PROGRESS_TAIL_MAX_BYTES = 4_096
TOOL_PROGRESS_TAIL_MAX_LINES = 20


class ToolExecutionProgress(WispEvent):
    """A transient snapshot of a running tool's output.

    Published only between ``tool.execution.started`` and ``tool.execution.ended``
    for the same call. Each event replaces the previous one, so a consumer may skip
    any of them and still show the latest state. Progress is never persisted and
    never reaches the model.
    """

    type: Literal["tool.execution.progress"] = "tool.execution.progress"
    call_id: str
    name: str
    # 1, 2, 3, ... per call; a consumer ignores a snapshot older than one it has.
    sequence: int = Field(ge=1)
    elapsed_ms: int = Field(ge=0)
    stdout_tail: str = Field(max_length=TOOL_PROGRESS_TAIL_MAX_BYTES)
    stderr_tail: str = Field(max_length=TOOL_PROGRESS_TAIL_MAX_BYTES)
    # Bytes each stream has produced so far, including those outside the tail.
    stdout_bytes: int = Field(ge=0)
    stderr_bytes: int = Field(ge=0)


class TrustRequested(WispEvent):
    type: Literal["trust.requested"] = "trust.requested"
    request_id: str
    project_path: Path


class TrustResolved(WispEvent):
    type: Literal["trust.resolved"] = "trust.resolved"
    request_id: str
    project_path: Path
    trusted: bool
    reason: str | None = None


class ProjectConfigApplied(WispEvent):
    """A trusted project's local config was applied mid-session.

    Emitted by the RPC process after a first-run trust approval rebuilds the runtime
    from the project's ``.wisp/settings.json``. It lets an out-of-process front-end
    (the TUI) refresh the provider/model/auth it displays and mutates, so its header
    and ``/provider`` / ``/model`` / ``/auth`` / ``/connect`` commands match the config
    the agent is actually running with. It also carries the effective automatic-
    compaction setting so frontends do not infer policy from their own startup state.

    ``effort`` carries the RPC agent's already-filtered, authoritative post-rebuild
    value (see the trusted-project rebuild in ``wisp.rpc.host``) rather than
    leaving the TUI to re-derive it from its own locally-tracked, already-once-filtered
    value -- the TUI's own copy was filtered against the untrusted-startup
    provider/model, so a tier invalid there but valid for the trusted project's
    provider/model would already be gone and unrecoverable from it. A single
    authoritative value avoids the two sides' filtering logic silently diverging.
    """

    type: Literal["project.config.applied"] = "project.config.applied"
    provider: str
    model: str | None = None
    effort: str | None = None
    auto_compaction_enabled: bool | None = None
    auth_path: Path


# The most edit locations one result carries; a call with more edits reports none.
MAX_EDIT_LOCATIONS = 256
# The most images one tool result carries, as many as one user message may.
MAX_TOOL_RESULT_IMAGES = 6
# The highest line an edit location may name, within every reader's 64-bit range.
MAX_EDIT_LINE = (1 << 63) - 1


class EditLocation(BaseModel):
    """Where one edit of an `edit` call landed, as 1-based first lines.

    Attributes:
        old_line (int): The edit's first line in the file before the call's edits.
        new_line (int): Its first line in the file the call produced, after the
            lines every edit above it added or removed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    old_line: int = Field(ge=1, le=MAX_EDIT_LINE)
    new_line: int = Field(ge=1, le=MAX_EDIT_LINE)


# The most calls one codemode script's result records; a script that makes more
# reports how many it left out. Scripts are limited to 200 calls by default.
MAX_NESTED_CALLS = 200
# Bounds on what one recorded call keeps: its arguments as JSON text, and its error
# or summary.
NESTED_CALL_ARGUMENTS_MAX_CHARS = 2_000
NESTED_CALL_TEXT_MAX_CHARS = 500


# The most model responses one tool result accounts for: enough for every subagent
# one `spawn_agent` call may run (8), each at its limit of 63 tool rounds and a
# final message.
MAX_TOOL_MODEL_RESPONSES = 512


class NestedToolCall(BaseModel):
    """One tool call a codemode script or a subagent made, as its result records it.

    Every call is recorded, including those refused before running and those the
    script or subagent ended before they finished, so a session shows everything
    it tried. Only the script or subagent sees the call's output; the model sees
    what the script prints and returns, or the subagent's final message.

    Attributes:
        call_id (str): The call's id, unique within its script or subagent.
        name (str): The tool that was called.
        target (str | None): The MCP tool a proxied `mcp_call` ran.
        arguments (JsonObject | None): The call's arguments; None when they were
            too large to keep.
        is_error (bool): Whether the call failed or was refused.
        failure_code (ToolFailureCode | None): Why it failed, when classified.
        error (str | None): The start of its error message.
        summary (str | None): A one-line summary, for read-type tools.
        started_at (datetime): When the call was made.
        duration_ms (int): How long it took, waiting for other calls included.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str
    name: str
    target: str | None = None
    arguments: JsonObject | None = None
    is_error: bool = False
    failure_code: ToolFailureCode | None = None
    error: str | None = Field(default=None, max_length=NESTED_CALL_TEXT_MAX_CHARS)
    summary: str | None = Field(default=None, max_length=NESTED_CALL_TEXT_MAX_CHARS)
    started_at: datetime
    duration_ms: int = Field(ge=0)


class ModelResponseUsage(BaseModel):
    """What one model response a tool requested itself used, such as a subagent's.

    Attributes:
        usage (TokenUsage): The tokens the response reported.
        cost (UsageCost | None): Its list-price estimate; None when the run had no
            cost estimator.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    usage: TokenUsage
    cost: UsageCost | None = None


class ToolNestedCallStarted(WispEvent):
    """A codemode script or a subagent made a tool call, which may still wait to run.

    Like `tool.execution.progress`, it is transient: published only between its
    parent's `tool.execution.started` and `tool.execution.ended`, never persisted
    and never sent to the model. Delivery is best effort. A call can start without
    ending when its parent ends first; the parent's `tool.execution.ended` closes
    it, and its `nested_calls` record every call, unless the run was cancelled
    while the parent ran, which leaves the result no calls to record.
    """

    type: Literal["tool.nested.started"] = "tool.nested.started"
    # The codemode or spawn_agent call that made this one.
    parent_call_id: str
    call_id: str
    name: str
    target: str | None = None
    arguments: JsonObject | None = None


class ToolNestedCallEnded(WispEvent):
    """A tool call a codemode script or a subagent made finished; transient, like its start."""

    type: Literal["tool.nested.ended"] = "tool.nested.ended"
    parent_call_id: str
    call: NestedToolCall


class _ToolResultEvent(_MessageOriginEvent):
    """Shared bounded result contract for distinct tool lifecycle events."""

    call_id: str
    name: str
    # The MCP tool a proxied `mcp_call` ran; `name` stays the tool the model called.
    target: str | None = None
    output: str
    is_error: bool
    # Typed recovery metadata for ordinary tool failures. These fields stay separate
    # from output so RPC consumers can classify failures without parsing prose.
    failure_code: ToolFailureCode | None = None
    retryable: bool = False
    recovery_hint: str | None = Field(default=None, max_length=500)
    # Process exit status for shell-like tools, promoted from ToolResult.data by
    # the executor. None for tools without exit-code semantics and error paths
    # that produced no ToolResult. This stays a narrow JSON-safe scalar rather
    # than exposing an extension-owned result mapping across the RPC boundary.
    exit_code: int | None = None
    # True only when output begins with Wisp's synthetic completion envelope.
    # Explicit provenance avoids parsing genuine legacy stdout that resembles it.
    output_has_exit_status: bool = False
    # A bounded pre-write snapshot for write-like tools. None for other tools,
    # creates, and overwrites whose previous contents could not be represented.
    before_text: str | None = None
    # Distinguishes a new file from an overwrite with no usable snapshot.
    created: bool = False
    # Where each of an `edit` call's edits landed, in call order, so a diff of the
    # edit can show real line numbers. Empty for other tools and failed edits.
    edit_locations: tuple[EditLocation, ...] = Field(default=(), max_length=MAX_EDIT_LOCATIONS)
    # A bounded one-line summary for successful read-type tools.
    summary: str | None = None
    # Whether the tool itself capped output before constructing this event.
    truncated: bool = False
    # Wall time the executor spent on the call, in milliseconds, excluding any wait
    # for approval. Executor-built failures (denials, invalid arguments) are timed
    # too; None only for results the loop settles itself, such as interrupted calls
    # and calls from a truncated response.
    duration_ms: int | None = Field(default=None, ge=0)
    # When the executor began the call, on the same terms as `duration_ms`: set
    # together with it, so a frontend can place parallel calls on one timeline.
    started_at: datetime | None = None
    # Resumable Bash metadata promoted from ToolResult.data for live JSON/RPC
    # consumers. These are bounded scalars/chunks, not the raw result mapping.
    process_id: str | None = None
    process_state: ManagedProcessState | None = None
    process_error: str | None = None
    stdout: str | None = None
    stderr: str | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_dropped_bytes: int = Field(default=0, ge=0)
    stderr_dropped_bytes: int = Field(default=0, ge=0)
    # When the managed process started, and when it reached a terminal state; the
    # end is None while it still runs.
    process_started_at: datetime | None = None
    process_ended_at: datetime | None = None
    # Images the tool returned, such as an image file `read` showed, by reference
    # into the session's image store; they are stored before the result is published.
    images: tuple[ImagePart, ...] = Field(default=(), max_length=MAX_TOOL_RESULT_IMAGES)
    # The calls a codemode script or a subagent made, in the order it made them, and
    # how many more it made than the result records. Empty for every other tool.
    nested_calls: tuple[NestedToolCall, ...] = Field(default=(), max_length=MAX_NESTED_CALLS)
    nested_calls_omitted: int = Field(default=0, ge=0)
    # The model responses a call's subagents completed, so their spend counts toward
    # the session's. Empty for every other tool.
    model_usage: tuple[ModelResponseUsage, ...] = Field(
        default=(), max_length=MAX_TOOL_MODEL_RESPONSES
    )

    @model_validator(mode="after")
    def _validate_failure_metadata(self) -> Self:
        has_failure_metadata = (
            self.failure_code is not None or self.retryable or self.recovery_hint is not None
        )
        if has_failure_metadata and not self.is_error:
            raise ValueError("Tool failure metadata requires is_error=true")
        if (self.retryable or self.recovery_hint is not None) and self.failure_code is None:
            raise ValueError("Retry metadata requires a tool failure code")
        if self.images and self.is_error:
            raise ValueError("A failed tool result carries no images")
        return self

    def _result_payload(self) -> JsonObject:
        """Return only fields declared by the shared result contract."""

        envelope_fields = WispEvent.model_fields
        return {
            name: getattr(self, name)
            for name in _ToolResultEvent.model_fields
            if name not in envelope_fields
        }


class ToolExecutionEnded(_ToolResultEvent):
    """Durable boundary reached after one tool execution finishes."""

    type: Literal["tool.execution.ended"] = "tool.execution.ended"


class ProcessEnded(WispEvent):
    """A process started with ``bash operation=start`` stopped on its own or by the user.

    Published whenever it happens, with or without a run in progress, so a
    frontend can close the process's card. A process the user stopped is published
    as ``cancelled``; one the agent cancelled publishes nothing, since the cancel's
    tool result already reports it.
    """

    type: Literal["process.ended"] = "process.ended"
    process_id: str = Field(min_length=1, max_length=128)
    # The command as started, cut to a summary for display.
    command: str = Field(max_length=PROCESS_COMMAND_SUMMARY_MAX_CHARS)
    state: EndedProcessState
    # The exit status, for a completed process.
    exit_code: int | None = None
    duration_ms: int = Field(ge=0)
    # Whether the model asked to be told; its notice arrives with the next prompt.
    notify: bool = False


class ToolResultReady(_ToolResultEvent):
    """Presentation/provider projection of a completed tool execution."""

    type: Literal["tool.result"] = "tool.result"

    @classmethod
    def from_execution_ended(cls, event: ToolExecutionEnded) -> Self:
        """Project a terminal execution without duplicating its payload schema."""

        return cls.model_validate(event._result_payload())
