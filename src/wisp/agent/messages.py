"""Message and session record contracts."""

from __future__ import annotations

import warnings
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from wisp.events import (
    CompactionReason,
    ContextBudget,
    FinishReason,
    JsonObject,
    MessageCompleted,
    TokenUsage,
    ToolCallSnapshot,
    ToolExecutionEnded,
    ToolResultReady,
    UsageCost,
    utc_now,
)

if TYPE_CHECKING:
    from wisp.sessions.entries import SessionEntry as PersistedSessionEntry
else:
    PersistedSessionEntry = object

Role = Literal["system", "user", "assistant", "tool"]


class Message(BaseModel):
    """A provider-facing chat message."""

    model_config = ConfigDict(frozen=True)

    role: Role
    content: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_calls: tuple[ToolCallSnapshot, ...] | None = None
    response_id: str | None = None
    finish_reason: FinishReason | None = None
    is_error: bool | None = None
    usage: TokenUsage | None = None
    cost: UsageCost | None = None
    created_at: datetime = Field(default_factory=utc_now)


class CompactionRecord(BaseModel):
    """Versioned payload describing an append-only context compaction."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[1, 2, 3, 4] = 4
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
    def _reject_v2_fields_on_v1(cls, data: object) -> object:
        if isinstance(data, dict):
            normalized = dict(data)
            if isinstance(normalized.get("replaced_entry_ids"), list):
                normalized["replaced_entry_ids"] = tuple(normalized["replaced_entry_ids"])
            if normalized.get("schema_version") == 1 and (
                "reason" in normalized or "trigger_budget" in normalized
            ):
                raise ValueError("compaction schema v1 cannot contain v2 metadata")
            schema_version = normalized.get("schema_version", 4)
            if type(schema_version) is int and schema_version < 4 and "cost" in normalized:
                raise ValueError("compaction schemas v1 through v3 cannot contain cost metadata")
            return normalized
        return data

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, summary: str) -> str:
        if not summary.strip():
            raise ValueError("compaction summary must not be blank")
        return summary

    @model_validator(mode="after")
    def _validate_reason_metadata(self) -> Self:
        if self.schema_version == 1:
            if self.reason != "manual" or self.trigger_budget is not None:
                raise ValueError("compaction schema v1 only supports manual records")
            return self
        if self.schema_version == 2 and self.reason == "overflow":
            raise ValueError("compaction schema v2 does not support overflow records")
        if self.reason in {"threshold", "overflow"} and self.trigger_budget is None:
            raise ValueError(f"{self.reason} compaction records require a trigger budget")
        if self.reason == "manual" and self.trigger_budget is not None:
            raise ValueError("manual compaction records must not include a trigger budget")
        return self

    @model_serializer(mode="wrap")
    def _serialize_versioned(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data = cast(dict[str, object], handler(self))
        if self.schema_version == 1:
            data.pop("reason", None)
            data.pop("trigger_budget", None)
        if self.schema_version < 4:
            data.pop("cost", None)
        return data


def message_from_completion_event(
    event: MessageCompleted | ToolExecutionEnded | ToolResultReady,
) -> Message:
    """Build the provider-visible message completed by a lifecycle event."""

    if isinstance(event, MessageCompleted):
        return Message(
            role=event.role,
            content=event.content,
            tool_calls=event.tool_calls,
            response_id=event.response_id,
            finish_reason=event.finish_reason,
            usage=event.usage,
            cost=event.cost,
            created_at=event.timestamp,
        )
    return Message(
        role="tool",
        content=event.output,
        tool_call_id=event.call_id,
        tool_name=event.name,
        is_error=event.is_error,
        created_at=event.timestamp,
    )


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


def provider_history_message(message: Message) -> Message | None:
    """Normalize one durable transcript message for provider replay."""

    if message.role == "tool":
        return historical_tool_observation(message)
    if message.role == "assistant" and message.tool_calls and not message.content.strip():
        return None
    return message


def SessionEntry(  # noqa: N802
    *,
    session_id: str,
    kind: Literal["message", "event", "compaction"] = "message",
    message: Message | None = None,
    event: JsonObject | None = None,
    compaction: CompactionRecord | None = None,
    id: str | None = None,
    operation_id: str | None = None,
    created_at: datetime | None = None,
) -> PersistedSessionEntry:
    """Build a concrete session entry through the deprecated flat-model API."""

    from wisp.sessions.entries import (
        CompactionSessionEntry,
        EventSessionEntry,
        MessageSessionEntry,
        PersistedEventEnvelope,
    )

    warnings.warn(
        "wisp.agent.messages.SessionEntry is deprecated; import a concrete entry "
        "model from wisp.sessions instead",
        DeprecationWarning,
        stacklevel=2,
    )
    payloads = {
        "message": message,
        "event": event,
        "compaction": compaction,
    }
    populated = tuple(name for name, payload in payloads.items() if payload is not None)
    if populated != (kind,):
        raise ValueError(f"{kind} session entries require exactly a {kind} payload")

    common: dict[str, object] = {
        "session_id": session_id,
        "operation_id": operation_id,
    }
    if id is not None:
        common["id"] = id
    if created_at is not None:
        common["created_at"] = created_at
    if kind == "message":
        assert message is not None
        return MessageSessionEntry.model_validate({**common, "message": message})
    if kind == "event":
        assert event is not None
        return EventSessionEntry.model_validate(
            {**common, "event": PersistedEventEnvelope(payload=event)}
        )
    assert compaction is not None
    return CompactionSessionEntry.model_validate({**common, "compaction": compaction})
