"""Schema-v3 events emitted by the Wisp agent core."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

EVENT_SCHEMA_VERSION = 3
JsonObject = dict[str, object]
MessageRole = Literal["system", "user", "assistant", "tool"]
RunOutcome = Literal["completed", "failed", "cancelled"]
FinishReason = Literal["stop", "tool_calls", "length", "error", "cancelled"]
RetryReason = Literal["network", "timeout", "rate_limit", "server_error", "transient_http"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class WispEvent(BaseModel):
    """Base class for versioned events consumed by every Wisp frontend."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str
    schema_version: Literal[3] = 3
    timestamp: datetime = Field(default_factory=utc_now)


class ToolCallSnapshot(BaseModel):
    """Serializable tool-call state attached to a completed assistant message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str
    name: str
    arguments: JsonObject
    parse_error: str | None = None


class AgentStarted(WispEvent):
    type: Literal["agent.started"] = "agent.started"
    session_id: str


class TurnStarted(WispEvent):
    type: Literal["turn.started"] = "turn.started"
    turn: int


class ProviderRetrying(WispEvent):
    """A provider request failed before streaming and will be retried."""

    type: Literal["provider.retrying"] = "provider.retrying"
    turn: int
    provider: str
    attempt: int
    max_attempts: int
    delay_seconds: float
    reason: RetryReason
    status_code: int | None = None


class MessageStarted(WispEvent):
    type: Literal["message.started"] = "message.started"
    turn: int
    role: MessageRole = "assistant"


class MessageDelta(WispEvent):
    type: Literal["message.delta"] = "message.delta"
    turn: int
    delta: str
    role: MessageRole = "assistant"
    content_index: int = 0
    content_kind: Literal["text", "thinking"] = "text"


class MessageCompleted(WispEvent):
    type: Literal["message.completed"] = "message.completed"
    turn: int
    content: str
    finish_reason: FinishReason
    role: MessageRole = "assistant"
    response_id: str | None = None
    tool_calls: tuple[ToolCallSnapshot, ...] = ()


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
    and ``/provider`` / ``/model`` / ``/auth`` / ``/login`` commands match the config
    the agent is actually running with.
    """

    type: Literal["project.config.applied"] = "project.config.applied"
    provider: str
    model: str | None = None
    auth_path: Path


class ToolExecutionEnded(WispEvent):
    type: Literal["tool.execution.ended"] = "tool.execution.ended"
    call_id: str
    name: str
    output: str
    is_error: bool


class ToolResultReady(WispEvent):
    type: Literal["tool.result"] = "tool.result"
    call_id: str
    name: str
    output: str
    is_error: bool


class TurnCompleted(WispEvent):
    type: Literal["turn.completed"] = "turn.completed"
    turn: int
    outcome: RunOutcome
    finish_reason: FinishReason


class SessionSaved(WispEvent):
    type: Literal["session.saved"] = "session.saved"
    session_id: str
    path: Path


class AgentCompleted(WispEvent):
    type: Literal["agent.completed"] = "agent.completed"
    session_id: str
    turns: int
    outcome: RunOutcome


class RpcCommandStarted(WispEvent):
    type: Literal["rpc.command.started"] = "rpc.command.started"
    command_id: str
    command_type: str


class RpcCommandFinished(WispEvent):
    type: Literal["rpc.command.finished"] = "rpc.command.finished"
    command_id: str
    command_type: str
    ok: bool
    error: str | None = None


class ErrorEvent(WispEvent):
    type: Literal["error"] = "error"
    message: str


type KnownWispEvent = Annotated[
    AgentStarted
    | TurnStarted
    | ProviderRetrying
    | MessageStarted
    | MessageDelta
    | MessageCompleted
    | ToolCallRequested
    | ToolExecutionStarted
    | ToolApprovalRequested
    | ToolApprovalResolved
    | TrustRequested
    | TrustResolved
    | ProjectConfigApplied
    | ToolExecutionEnded
    | ToolResultReady
    | TurnCompleted
    | SessionSaved
    | AgentCompleted
    | RpcCommandStarted
    | RpcCommandFinished
    | ErrorEvent,
    Field(discriminator="type"),
]
KnownWispEventAdapter: TypeAdapter[KnownWispEvent] = TypeAdapter(KnownWispEvent)
JsonObjectAdapter: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


def _require_schema_v3(data: JsonObject) -> None:
    version = data.get("schema_version")
    if version != EVENT_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported Wisp event schema_version: {version!r}; expected {EVENT_SCHEMA_VERSION}"
        )


def wisp_event_from_json(line: str) -> KnownWispEvent:
    """Parse one schema-v3 JSONL event line into a typed Wisp event."""

    data = JsonObjectAdapter.validate_json(line)
    _require_schema_v3(data)
    return KnownWispEventAdapter.validate_python(data)


def wisp_event_from_dict(data: JsonObject) -> KnownWispEvent:
    """Parse one schema-v3 event dictionary into a typed Wisp event."""

    _require_schema_v3(data)
    return KnownWispEventAdapter.validate_python(data)
