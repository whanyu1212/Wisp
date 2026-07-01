"""Message and session record contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from wisp.events import utc_now

Role = Literal["system", "user", "assistant", "tool"]


class Message(BaseModel):
    """A provider-facing chat message."""

    model_config = ConfigDict(frozen=True)

    role: Role
    content: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class SessionEntry(BaseModel):
    """One append-only JSONL record in a Wisp session."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    session_id: str
    kind: Literal["message"] = "message"
    message: Message
    created_at: datetime = Field(default_factory=utc_now)
