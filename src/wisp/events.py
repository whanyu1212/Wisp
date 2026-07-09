"""Events emitted by the Wisp agent core."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

JsonObject = dict[str, object]


def utc_now() -> datetime:
    return datetime.now(UTC)


class WispEvent(BaseModel):
    """Base class for events consumed by CLI, JSON, and future TUI renderers."""

    model_config = ConfigDict(frozen=True)

    type: str
    timestamp: datetime = Field(default_factory=utc_now)


class AgentStarted(WispEvent):
    type: Literal["agent.started"] = "agent.started"
    session_id: str


class TokenDelta(WispEvent):
    type: Literal["token.delta"] = "token.delta"
    delta: str


class AssistantMessage(WispEvent):
    type: Literal["assistant.message"] = "assistant.message"
    content: str


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


class SessionSaved(WispEvent):
    type: Literal["session.saved"] = "session.saved"
    session_id: str
    path: Path


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
    | TokenDelta
    | AssistantMessage
    | ToolCallRequested
    | ToolExecutionStarted
    | ToolApprovalRequested
    | ToolApprovalResolved
    | TrustRequested
    | TrustResolved
    | ProjectConfigApplied
    | ToolExecutionEnded
    | ToolResultReady
    | SessionSaved
    | RpcCommandStarted
    | RpcCommandFinished
    | ErrorEvent,
    Field(discriminator="type"),
]
KnownWispEventAdapter: TypeAdapter[KnownWispEvent] = TypeAdapter(KnownWispEvent)


def wisp_event_from_json(line: str) -> KnownWispEvent:
    """Parse one JSONL event line into a typed Wisp event."""

    return KnownWispEventAdapter.validate_json(line)


def wisp_event_from_dict(data: JsonObject) -> KnownWispEvent:
    """Parse one event dictionary into a typed Wisp event."""

    return KnownWispEventAdapter.validate_python(data)
