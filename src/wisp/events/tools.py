"""Tool call, approval, trust, and tool-result lifecycle events."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from wisp.events._base import JsonObject, ManagedProcessState, WispEvent, _MessageOriginEvent
from wisp.tool_types import ToolFailureCode


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


class ToolApprovalRequested(WispEvent):
    type: Literal["tool.approval.requested"] = "tool.approval.requested"
    call_id: str
    name: str
    arguments: JsonObject
    safety: Literal["read", "mutating", "command"]


class ToolApprovalResolved(WispEvent):
    type: Literal["tool.approval.resolved"] = "tool.approval.resolved"
    call_id: str
    name: str
    approved: bool
    reason: str | None = None


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


class _ToolResultEvent(_MessageOriginEvent):
    """Shared bounded result contract for distinct tool lifecycle events."""

    call_id: str
    name: str
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
    # A bounded one-line summary for successful read-type tools.
    summary: str | None = None
    # Whether the tool itself capped output before constructing this event.
    truncated: bool = False
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

    @model_validator(mode="after")
    def _validate_failure_metadata(self) -> Self:
        has_failure_metadata = (
            self.failure_code is not None or self.retryable or self.recovery_hint is not None
        )
        if has_failure_metadata and not self.is_error:
            raise ValueError("Tool failure metadata requires is_error=true")
        if (self.retryable or self.recovery_hint is not None) and self.failure_code is None:
            raise ValueError("Retry metadata requires a tool failure code")
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


class ToolResultReady(_ToolResultEvent):
    """Presentation/provider projection of a completed tool execution."""

    type: Literal["tool.result"] = "tool.result"

    @classmethod
    def from_execution_ended(cls, event: ToolExecutionEnded) -> Self:
        """Project a terminal execution without duplicating its payload schema."""

        return cls.model_validate(event._result_payload())
