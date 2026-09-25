"""Versioned session-entry persistence contracts and decoding."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Annotated, Literal, Self, TypeGuard
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from wisp.agent.messages import CompactionRecord, Message
from wisp.events import (
    JsonObject,
    JsonObjectAdapter,
    KnownWispEvent,
    ToolPresentationStatus,
    utc_now,
    wisp_event_from_dict,
)
from wisp.sessions.errors import (
    MalformedPersistedEventError,
    MalformedSessionEntryError,
    UnsupportedPersistedEventVersionError,
    UnsupportedSessionEntryVersionError,
)

SESSION_ENTRY_SCHEMA_VERSION: Literal[6] = 6
PERSISTED_EVENT_ENVELOPE_SCHEMA_VERSION: Literal[1] = 1
MAX_SESSION_NAME_BYTES = 256
_SESSION_NAME_NEWLINES_RE = re.compile(r"[\r\n]+")


class SessionEntryBase(BaseModel):
    """Fields shared by every current persisted session entry."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[6] = SESSION_ENTRY_SCHEMA_VERSION
    id: str = Field(default_factory=lambda: uuid4().hex, min_length=1)
    session_id: str = Field(min_length=1)
    operation_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class SessionTreeEntryBase(SessionEntryBase):
    """Fields shared by entries that participate in the session tree."""

    parent_id: str | None = None


class ToolResultPresentationSnapshot(BaseModel):
    """UI-only metadata for reconstructing a resolved historical tool card."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    status: ToolPresentationStatus | None = None
    exit_code: int | None = None
    output_has_exit_status: bool = False
    before_text: str | None = None
    created: bool = False
    summary: str | None = None
    truncated: bool = False


class MessageSessionEntry(SessionTreeEntryBase):
    """One provider/frontend-visible message record."""

    kind: Literal["message"] = "message"
    message: Message
    tool_result: ToolResultPresentationSnapshot | None = None

    @model_validator(mode="after")
    def _validate_tool_result(self) -> Self:
        if self.tool_result is not None and self.message.role != "tool":
            raise ValueError("tool-result presentation metadata is valid only on tool messages")
        return self


class PersistedEventEnvelope(BaseModel):
    """Versioned envelope retaining an event's original raw payload."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[1] = PERSISTED_EVENT_ENVELOPE_SCHEMA_VERSION
    payload: JsonObject


class EventSessionEntry(SessionTreeEntryBase):
    """One raw runtime event retained for audit and optional typed access."""

    kind: Literal["event"] = "event"
    event: PersistedEventEnvelope


class CompactionSessionEntry(SessionTreeEntryBase):
    """One append-only provider-context compaction record."""

    kind: Literal["compaction"] = "compaction"
    compaction: CompactionRecord


class ActiveLeafSessionEntry(SessionEntryBase):
    """Append-only selection of the branch used for subsequent replay/appends."""

    kind: Literal["active_leaf"] = "active_leaf"
    previous_leaf_id: str | None
    active_leaf_id: str | None
    reason: Literal["system", "navigation", "unrevert"] = "system"
    selected_entry_id: str | None = Field(default=None, min_length=1)
    source_transition_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate_transition_metadata(self) -> Self:
        if self.reason == "system":
            if self.selected_entry_id is not None or self.source_transition_id is not None:
                raise ValueError("system active-leaf transitions cannot reference user actions")
        elif self.reason == "navigation":
            if self.selected_entry_id is None or self.source_transition_id is not None:
                raise ValueError(
                    "navigation active-leaf transitions require selected_entry_id only"
                )
        elif self.source_transition_id is None or self.selected_entry_id is not None:
            raise ValueError("unrevert active-leaf transitions require source_transition_id only")
        return self


class SessionInfoSessionEntry(SessionEntryBase):
    """Append-only session metadata that does not participate in the tree."""

    kind: Literal["session_info"] = "session_info"
    name: str | None

    @field_validator("name", mode="before")
    @classmethod
    def _normalize_name(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        normalized = normalize_session_name(value)
        if normalized is None:
            return None
        if len(normalized.encode("utf-8")) > MAX_SESSION_NAME_BYTES:
            raise ValueError(f"session name cannot exceed {MAX_SESSION_NAME_BYTES} UTF-8 bytes")
        return normalized


type SessionTreeEntry = MessageSessionEntry | EventSessionEntry | CompactionSessionEntry


type SessionEntry = Annotated[
    MessageSessionEntry
    | EventSessionEntry
    | CompactionSessionEntry
    | ActiveLeafSessionEntry
    | SessionInfoSessionEntry,
    Field(discriminator="kind"),
]

SessionEntryAdapter: TypeAdapter[SessionEntry] = TypeAdapter(SessionEntry)


def is_session_tree_entry(entry: SessionEntry) -> TypeGuard[SessionTreeEntry]:
    """Return whether an entry participates in parent-linked session history."""

    return isinstance(
        entry,
        (MessageSessionEntry, EventSessionEntry, CompactionSessionEntry),
    )


def session_entry_to_json(entry: SessionEntry) -> str:
    """Serialize one entry while retaining structural null references explicitly."""

    raw = entry.model_dump(mode="json", exclude_none=True)
    if is_session_tree_entry(entry):
        raw["parent_id"] = entry.parent_id
    elif isinstance(entry, ActiveLeafSessionEntry):
        raw["previous_leaf_id"] = entry.previous_leaf_id
        raw["active_leaf_id"] = entry.active_leaf_id
    elif isinstance(entry, SessionInfoSessionEntry):
        raw["name"] = entry.name
    return json.dumps(raw, ensure_ascii=False, separators=(",", ":"))


def normalize_session_name(name: str) -> str | None:
    """Normalize user-facing session names before persistence."""

    normalized = _SESSION_NAME_NEWLINES_RE.sub(" ", name).strip()
    return normalized or None


def session_entry_from_json(line: str, *, source: str | None = None) -> SessionEntry:
    """Decode one current-schema JSONL entry without rewriting its source.

    Args:
        line (str): One JSONL record.
        source (str | None): Location included in error messages.

    Returns:
        SessionEntry: The validated entry.

    Raises:
        MalformedSessionEntryError: The record is not a valid current entry.
        UnsupportedSessionEntryVersionError: The record uses another entry schema.
        MalformedPersistedEventError: An event envelope has a non-integer version.
        UnsupportedPersistedEventVersionError: An event envelope uses another schema.
    """

    location = f" at {source}" if source is not None else ""
    try:
        raw = JsonObjectAdapter.validate_json(line)
    except ValidationError as exc:
        raise MalformedSessionEntryError(f"Malformed session entry JSON{location}") from exc
    return session_entry_from_dict(raw, source=source)


def session_entry_from_dict(raw: JsonObject, *, source: str | None = None) -> SessionEntry:
    """Decode one current-schema entry dictionary.

    Entries written in any other ``schema_version`` are rejected rather than
    upgraded.

    Args:
        raw (JsonObject): One decoded JSONL record.
        source (str | None): Location included in error messages.

    Returns:
        SessionEntry: The validated entry.

    Raises:
        MalformedSessionEntryError: The record is not a valid current entry.
        UnsupportedSessionEntryVersionError: The record uses another entry schema.
        MalformedPersistedEventError: An event envelope has a non-integer version.
        UnsupportedPersistedEventVersionError: An event envelope uses another schema.
    """

    location = f" at {source}" if source is not None else ""
    _require_current_entry_version(raw, location=location)
    normalized = _restore_omitted_null_references(raw)
    _require_persisted_base_fields(normalized, location=location)
    if normalized.get("kind") == "active_leaf" and "reason" not in normalized:
        raise MalformedSessionEntryError(f"Active-leaf session entries require reason{location}")
    _require_supported_event_envelope(normalized, location=location)
    try:
        # Validate through Pydantic's JSON path so strict models still accept
        # JSON-native datetime strings while rejecting Python-side coercions.
        return SessionEntryAdapter.validate_json(json.dumps(normalized))
    except (TypeError, ValidationError) as exc:
        raise MalformedSessionEntryError(f"Malformed session entry{location}") from exc


def _require_current_entry_version(raw: JsonObject, *, location: str) -> None:
    if "schema_version" not in raw:
        raise UnsupportedSessionEntryVersionError(
            f"Session entry has no schema_version{location}; "
            f"expected {SESSION_ENTRY_SCHEMA_VERSION}"
        )
    version = raw["schema_version"]
    if type(version) is not int:
        raise MalformedSessionEntryError(
            f"Session entry schema_version must be an integer{location}"
        )
    if version != SESSION_ENTRY_SCHEMA_VERSION:
        raise UnsupportedSessionEntryVersionError(
            f"Unsupported session entry schema_version {version}{location}; "
            f"expected {SESSION_ENTRY_SCHEMA_VERSION}"
        )


def _require_persisted_base_fields(raw: JsonObject, *, location: str) -> None:
    """Prevent defaults from manufacturing unstable persisted identity or time."""

    missing = tuple(field for field in ("id", "session_id", "created_at") if field not in raw)
    if missing:
        fields = ", ".join(missing)
        raise MalformedSessionEntryError(
            f"Persisted session entry is missing required field(s) {fields}{location}"
        )


def _restore_omitted_null_references(raw: JsonObject) -> JsonObject:
    """Restore null references omitted by public exclude-none serialization."""

    normalized = dict(raw)
    kind = raw.get("kind")
    if kind in {"message", "event", "compaction"}:
        normalized.setdefault("parent_id", None)
    elif kind == "active_leaf":
        normalized.setdefault("previous_leaf_id", None)
        normalized.setdefault("active_leaf_id", None)
    return normalized


def _require_supported_event_envelope(raw: JsonObject, *, location: str) -> None:
    """Classify event-envelope version errors before union validation."""

    if raw.get("kind") != "event":
        return
    event = raw.get("event")
    if not isinstance(event, dict):
        return
    version = event.get("schema_version")
    if type(version) is not int:
        raise MalformedPersistedEventError(
            f"Persisted event envelope schema_version must be an integer{location}"
        )
    if version != PERSISTED_EVENT_ENVELOPE_SCHEMA_VERSION:
        raise UnsupportedPersistedEventVersionError(
            f"Unsupported persisted event envelope schema_version {version}{location}; "
            f"expected {PERSISTED_EVENT_ENVELOPE_SCHEMA_VERSION}"
        )


def typed_event_from_envelope(
    envelope: PersistedEventEnvelope,
    *,
    source: str | None = None,
) -> KnownWispEvent:
    """Validate one retained raw event only when typed access is requested."""

    location = f" at {source}" if source is not None else ""
    try:
        return wisp_event_from_dict(envelope.payload)
    except (ValidationError, ValueError) as exc:
        raise MalformedPersistedEventError(f"Malformed persisted event{location}") from exc
