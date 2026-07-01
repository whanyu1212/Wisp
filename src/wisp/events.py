"""Events emitted by the Wisp agent core."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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


class ErrorEvent(WispEvent):
    type: Literal["error"] = "error"
    message: str
