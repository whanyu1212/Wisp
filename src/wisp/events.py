"""Events emitted by the Wisp agent core."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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


class SessionSaved(WispEvent):
    type: Literal["session.saved"] = "session.saved"
    session_id: str
    path: Path


class ErrorEvent(WispEvent):
    type: Literal["error"] = "error"
    message: str
