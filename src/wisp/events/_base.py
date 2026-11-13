"""Event envelope, shared literal vocabularies, and snapshot types used across lifecycles."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

JsonObject = dict[str, object]
MessageRole = Literal["system", "user", "assistant", "tool"]
RunOutcome = Literal["completed", "failed", "cancelled"]
FinishReason = Literal["stop", "tool_calls", "length", "error", "cancelled"]
RetryReason = Literal["network", "timeout", "rate_limit", "server_error", "transient_http"]
CompactionReason = Literal["manual", "threshold", "overflow"]
QueueMode = Literal["one_at_a_time", "all"]
QueueKind = Literal["steering", "follow_up"]
ToolPresentationStatus = Literal["done", "error", "denied", "cancelled"]
ManagedProcessState = Literal["running", "completed", "failed", "timed_out", "cancelled"]
# How a started process stopped on its own: every terminal state except cancelled.
EndedProcessState = Literal["completed", "failed", "timed_out"]
ImageMediaType = Literal["image/png", "image/jpeg", "image/gif", "image/webp"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class WispEvent(BaseModel):
    """Base class for the typed events consumed by every Wisp frontend.

    Events carry no per-event version. ``schemas/live-rpc/`` describes the
    current release's events; backend and frontend must be the same release.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str
    timestamp: datetime = Field(default_factory=utc_now)


class ToolCallSnapshot(BaseModel):
    """Serializable tool-call state attached to a completed assistant message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str
    name: str
    arguments: JsonObject
    # Runtime-only provider metadata used to reconstruct a fresh active tool
    # exchange. It is intentionally absent from persisted/public events.
    provider_call_id: str | None = Field(default=None, exclude=True, repr=False)
    parse_error: str | None = None


class ImagePart(BaseModel):
    """A reference to one validated image held in a session's image store.

    The bytes live outside the transcript, addressed by their SHA-256 digest, so
    messages and events carry only this description of them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    media_type: ImageMediaType
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class _MessageOriginEvent(WispEvent):
    """Associate live presentation with an assigned session message identity.

    The session assigns the identity before publishing the event. Persistence can
    still be buffered or fail; consumers must verify the entry in durable history
    before treating it as recoverable.
    """

    message_entry_id: str | None = Field(default=None, min_length=1, max_length=4096)
