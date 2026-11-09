"""Provider-facing message and compaction records, and their projection from events."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from wisp.events import (
    CompactionReason,
    ContextBudget,
    ContextObservation,
    FinishReason,
    ImagePart,
    JsonObject,
    MessageCompleted,
    TokenUsage,
    ToolCallSnapshot,
    ToolExecutionEnded,
    UsageCost,
    utc_now,
)
from wisp.skills.models import SkillInvocationEvidence

Role = Literal["system", "user", "assistant", "tool"]


class NativeOutput(BaseModel):
    """A provider's own output items for one response, kept for exact replay.

    The items are opaque to everything except the provider adapter that
    produced them. Replaying them lets a later request rebuild the response
    exactly as its own run sent it, so the provider's prompt cache still matches.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    items: tuple[JsonObject, ...] = Field(min_length=1)


class Message(BaseModel):
    """A provider-facing chat message."""

    model_config = ConfigDict(frozen=True)

    role: Role
    content: str
    # Images the user attached, or a tool returned, in order; None rather than
    # empty so messages without images serialize exactly as before.
    images: tuple[ImagePart, ...] | None = Field(default=None, min_length=1)
    # The bytes of `images`, in order, loaded only for a request to a model that
    # takes images; never persisted or shown. Adapters send a text placeholder for
    # images whose bytes are absent.
    image_bytes: tuple[bytes, ...] | None = Field(default=None, exclude=True, repr=False)
    prompt_cache_boundary: bool = Field(default=False, exclude=True, repr=False)
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_calls: tuple[ToolCallSnapshot, ...] | None = None
    response_id: str | None = None
    finish_reason: FinishReason | None = None
    is_error: bool | None = None
    usage: TokenUsage | None = None
    cost: UsageCost | None = None
    context_observation: ContextObservation | None = None
    skill_invocation: SkillInvocationEvidence | None = None
    native_output: NativeOutput | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _validate_images(self) -> Self:
        if self.images is not None and self.role not in ("user", "tool"):
            raise ValueError("images are valid only on user and tool messages")
        # Adapters route an unpaired message with images as user input.
        if self.images is not None and self.role == "tool" and not self.tool_call_id:
            raise ValueError("a tool message with images needs its tool_call_id")
        if self.image_bytes is not None and len(self.image_bytes) != len(self.images or ()):
            raise ValueError("image_bytes must hold exactly one entry per image")
        return self

    @property
    def user_visible_content(self) -> str:
        """Return submitted text rather than any provider-only expansion."""

        if self.skill_invocation is not None:
            return self.skill_invocation.original_content
        return self.content


class CompactionRecord(BaseModel):
    """Versioned payload describing an append-only context compaction."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[4] = 4
    summary: str
    replaced_entry_ids: tuple[str, ...] = Field(min_length=1)
    provider: str
    model: str | None = None
    instructions: str | None = None
    usage: TokenUsage | None = None
    cost: UsageCost | None = None
    reason: CompactionReason = "manual"
    trigger_budget: ContextBudget | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_replaced_entry_id_list(cls, data: object) -> object:
        if isinstance(data, dict) and isinstance(data.get("replaced_entry_ids"), list):
            return {**data, "replaced_entry_ids": tuple(data["replaced_entry_ids"])}
        return data

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, summary: str) -> str:
        if not summary.strip():
            raise ValueError("compaction summary must not be blank")
        return summary

    @model_validator(mode="after")
    def _validate_reason_metadata(self) -> Self:
        if self.reason in {"threshold", "overflow"} and self.trigger_budget is None:
            raise ValueError(f"{self.reason} compaction records require a trigger budget")
        if self.reason == "manual" and self.trigger_budget is not None:
            raise ValueError("manual compaction records must not include a trigger budget")
        return self


def queued_message_images(messages: Sequence[Message]) -> tuple[tuple[ImagePart, ...], ...]:
    """Return each queued message's images, or nothing when none carries images.

    Queue events align this with their message texts and leave it empty for
    text-only queues, so their existing payloads stay unchanged.

    Args:
        messages (Sequence[Message]): Queued messages, in queue order.

    Returns:
        tuple[tuple[ImagePart, ...], ...]: One entry per message, or ().
    """

    if not any(message.images for message in messages):
        return ()
    return tuple(message.images or () for message in messages)


def completion_event_has_history(
    event: MessageCompleted | ToolExecutionEnded,
) -> bool:
    """Return whether a completion carries portable provider history."""

    return not (
        isinstance(event, MessageCompleted)
        and event.finish_reason in {"error", "cancelled"}
        and not event.content
    )


def message_from_completion_event(
    event: MessageCompleted | ToolExecutionEnded,
) -> Message:
    """Build a detached provider-visible message from a lifecycle event.

    Args:
        event (MessageCompleted | ToolExecutionEnded): Completed assistant or
            tool output to retain before exposing the event to consumers.

    Returns:
        Message: Message whose nested data cannot be mutated through the event.
    """

    if isinstance(event, MessageCompleted):
        return Message(
            role=event.role,
            content=event.content,
            tool_calls=event.tool_calls,
            response_id=event.response_id,
            finish_reason=event.finish_reason,
            usage=event.usage,
            cost=event.cost,
            context_observation=event.context_observation,
            created_at=event.timestamp,
        ).model_copy(deep=True)
    return Message(
        role="tool",
        content=event.output,
        tool_call_id=event.call_id,
        tool_name=event.name,
        is_error=event.is_error,
        images=event.images or None,
        created_at=event.timestamp,
    )


__all__ = [
    "Message",
    "CompactionRecord",
    "NativeOutput",
    "Role",
    "completion_event_has_history",
    "queued_message_images",
    "message_from_completion_event",
]
