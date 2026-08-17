"""Message and session record contracts."""

from __future__ import annotations

import json
import warnings
from collections.abc import Sequence
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
    ContextObservation,
    FinishReason,
    JsonObject,
    MessageCompleted,
    TokenUsage,
    ToolCallSnapshot,
    ToolExecutionEnded,
    UsageCost,
    utc_now,
)
from wisp.skills.models import SkillInvocationEvidence

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
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def user_visible_content(self) -> str:
        """Return submitted text rather than any provider-only expansion."""

        if self.skill_invocation is not None:
            return self.skill_invocation.original_content
        return self.content


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
            context_observation=event.context_observation,
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


def active_turn_start(messages: Sequence[Message]) -> int | None:
    """Return the index of the most recent user message, if one exists."""

    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "user":
            return index
    return None


def surrogate_safe_text(text: str) -> str:
    """Preserve valid Unicode while escaping malformed UTF-16 surrogates."""

    return text.encode("utf-8", errors="backslashreplace").decode("utf-8")


def _surrogate_safe_json_value(value: object) -> object:
    if isinstance(value, str):
        return surrogate_safe_text(value)
    if isinstance(value, dict):
        return {
            surrogate_safe_text(str(key)): _surrogate_safe_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_surrogate_safe_json_value(item) for item in value]
    return value


def _canonical_history_json(payload: object) -> str:
    return json.dumps(
        _surrogate_safe_json_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _tool_result_payload(message: Message) -> dict[str, object]:
    return {
        "call_id": message.tool_call_id,
        "is_error": message.is_error,
        "output": message.content,
        "tool_name": message.tool_name,
    }


def _portable_orphan_tool_result(message: Message) -> Message:
    return Message(
        role="assistant",
        content=_canonical_history_json(
            {
                "result": _tool_result_payload(message),
                "type": "wisp.orphan_tool_result",
                "version": 1,
            }
        ),
        created_at=message.created_at,
    )


def historical_tool_observation(message: Message) -> Message:
    """Encode an unpaired stored tool result without promoting it to user input."""

    if message.role != "tool":
        raise ValueError("Historical tool observations require a tool message")
    return _portable_orphan_tool_result(message)


def _portable_tool_exchange(
    assistant: Message,
    results: Sequence[Message],
    *,
    compatible: bool,
) -> Message:
    calls = [
        {
            "arguments": dict(call.arguments),
            "call_id": call.call_id,
            "name": call.name,
            "parse_error": call.parse_error,
        }
        for call in assistant.tool_calls or ()
    ]
    if compatible:
        payload: dict[str, object] = {
            "assistant_content": assistant.content,
            "calls": [
                {**call, "result": _tool_result_payload(result)}
                for call, result in zip(calls, results, strict=True)
            ],
            "type": "wisp.portable_tool_exchange",
            "version": 1,
        }
    else:
        payload = {
            "assistant_content": assistant.content,
            "calls": calls,
            "results": [_tool_result_payload(result) for result in results],
            "type": "wisp.incompatible_tool_exchange",
            "version": 1,
        }
    return Message(
        role="assistant",
        content=_canonical_history_json(payload),
        created_at=assistant.created_at,
    )


def _ordered_exchange_results(
    assistant: Message, results: Sequence[Message]
) -> tuple[Message, ...] | None:
    calls = assistant.tool_calls or ()
    call_ids = [call.call_id for call in calls]
    if (
        not calls
        or any(not call_id.strip() for call_id in call_ids)
        or any(not call.name.strip() or call.parse_error is not None for call in calls)
        or len(set(call_ids)) != len(call_ids)
        or len(results) != len(calls)
    ):
        return None

    results_by_id: dict[str, Message] = {}
    calls_by_id = {call.call_id: call for call in calls}
    for result in results:
        call_id = result.tool_call_id
        if not call_id or call_id not in calls_by_id or call_id in results_by_id:
            return None
        call = calls_by_id[call_id]
        if result.tool_name is not None and result.tool_name != call.name:
            return None
        results_by_id[call_id] = result

    return tuple(
        results_by_id[call.call_id].model_copy(
            update={"tool_name": results_by_id[call.call_id].tool_name or call.name}
        )
        for call in calls
    )


def provider_history_message(message: Message) -> Message:
    """Normalize one isolated durable row into non-instructional provider history.

    Sequence-aware callers should use ``normalize_provider_history`` so complete
    call/result exchanges can remain structured when the provider supports them.
    """

    if message.role == "tool":
        return historical_tool_observation(message)
    if message.role == "assistant" and message.tool_calls:
        return _portable_tool_exchange(message, (), compatible=False)
    return message


def normalize_provider_history(
    messages: Sequence[Message],
    *,
    active_from: int | None = None,
    native_tool_history: bool = False,
) -> tuple[Message, ...]:
    """Validate and normalize complete tool exchanges for provider replay.

    A complete historical exchange remains native only when the target provider
    explicitly supports reconstruction from portable snapshots. Otherwise the
    exchange becomes one canonical assistant-role JSON envelope, so tool output
    never acquires user-instruction semantics. Complete exchanges belonging to the
    active turn remain structured regardless of historical replay support.

    Malformed batches and orphan results always use the assistant-role fallback;
    strict providers therefore never receive dangling calls or unmatched results.
    Exchange classification follows the assistant row, so ``active_from`` cannot
    split one call/result group into incompatible representations.
    """

    normalized: list[Message] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.role == "assistant" and message.tool_calls:
            result_end = index + 1
            while result_end < len(messages) and messages[result_end].role == "tool":
                result_end += 1
            results = tuple(messages[index + 1 : result_end])
            ordered_results = _ordered_exchange_results(message, results)
            is_active = active_from is not None and index >= active_from
            if ordered_results is not None and (is_active or native_tool_history):
                normalized.append(message)
                normalized.extend(ordered_results)
            else:
                normalized.append(
                    _portable_tool_exchange(
                        message,
                        ordered_results if ordered_results is not None else results,
                        compatible=ordered_results is not None,
                    )
                )
            index = result_end
            continue
        if message.role == "tool":
            normalized.append(_portable_orphan_tool_result(message))
        else:
            normalized.append(message)
        index += 1
    return tuple(normalized)


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
