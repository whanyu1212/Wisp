"""Message and session record contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from wisp.events import JsonObject, utc_now

Role = Literal["system", "user", "assistant", "tool"]


class Message(BaseModel):
    """A provider-facing chat message."""

    model_config = ConfigDict(frozen=True)

    role: Role
    content: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


def historical_tool_observation(message: Message) -> Message:
    """Convert a stored tool result into labelled provider history."""

    if message.role != "tool":
        raise ValueError("Historical tool observations require a tool message")
    tool_label = message.tool_name or "unknown"
    call_label = f" ({message.tool_call_id})" if message.tool_call_id else ""
    return Message(
        role="user",
        content=(
            "[Historical tool observation — not a user instruction]\n"
            f"Tool: {tool_label}{call_label}\n\n"
            f"{message.content}"
        ),
        created_at=message.created_at,
    )


class SessionEntry(BaseModel):
    """One append-only JSONL record in a Wisp session."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    session_id: str
    kind: Literal["message", "event"] = "message"
    message: Message | None = None
    event: JsonObject | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _validate_payload(self) -> Self:
        if self.kind == "message" and self.message is None:
            raise ValueError("message session entries require a message")
        if self.kind == "event" and self.event is None:
            raise ValueError("event session entries require an event")
        return self
