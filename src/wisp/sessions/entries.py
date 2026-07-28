"""Versioned session-entry persistence contracts and compatibility decoding."""

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
    EVENT_SCHEMA_VERSION,
    JsonObject,
    JsonObjectAdapter,
    KnownWispEvent,
    utc_now,
    wisp_event_from_dict,
)
from wisp.sessions.errors import (
    MalformedPersistedEventError,
    MalformedSessionEntryError,
    UnsupportedPersistedEventVersionError,
    UnsupportedSessionEntryVersionError,
)

SESSION_ENTRY_SCHEMA_VERSION: Literal[3] = 3
PERSISTED_EVENT_ENVELOPE_SCHEMA_VERSION: Literal[1] = 1
_MIN_SUPPORTED_EVENT_SCHEMA_VERSION = 5
MAX_SESSION_NAME_BYTES = 256
_SESSION_NAME_NEWLINES_RE = re.compile(r"[\r\n]+")


class SessionEntryBase(BaseModel):
    """Fields shared by every current persisted session entry."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[3] = SESSION_ENTRY_SCHEMA_VERSION
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

    exit_code: int | None = None
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


def session_entry_from_json(
    line: str,
    *,
    source: str | None = None,
    legacy_parent_id: str | None = None,
) -> SessionEntry:
    """Decode one current or legacy JSONL entry without rewriting its source."""

    location = f" at {source}" if source is not None else ""
    try:
        raw = JsonObjectAdapter.validate_json(line)
    except ValidationError as exc:
        raise MalformedSessionEntryError(f"Malformed session entry JSON{location}") from exc
    return session_entry_from_dict(
        raw,
        source=source,
        legacy_parent_id=legacy_parent_id,
    )


def session_entry_from_dict(
    raw: JsonObject,
    *,
    source: str | None = None,
    legacy_parent_id: str | None = None,
) -> SessionEntry:
    """Decode one entry dictionary through its explicit compatibility path."""

    location = f" at {source}" if source is not None else ""
    if "schema_version" not in raw:
        normalized = _upgrade_legacy_entry(
            raw,
            source=source,
            parent_id=legacy_parent_id,
        )
    else:
        version = raw["schema_version"]
        if type(version) is not int:
            raise MalformedSessionEntryError(
                f"Session entry schema_version must be an integer{location}"
            )
        if version == 1:
            normalized = _upgrade_v1_entry(
                raw,
                source=source,
                parent_id=legacy_parent_id,
            )
        elif version == 2:
            if raw.get("kind") == "session_info":
                raise MalformedSessionEntryError(
                    f"Unknown v2 session entry kind 'session_info'{location}"
                )
            normalized = _normalize_v2_structural_fields(
                raw,
                parent_id=legacy_parent_id,
            )
            normalized["schema_version"] = SESSION_ENTRY_SCHEMA_VERSION
        elif version != SESSION_ENTRY_SCHEMA_VERSION:
            raise UnsupportedSessionEntryVersionError(
                f"Unsupported session entry schema_version {version}{location}; "
                f"expected {SESSION_ENTRY_SCHEMA_VERSION}"
            )
        else:
            normalized = _normalize_v2_structural_fields(
                raw,
                parent_id=legacy_parent_id,
            )
    _require_persisted_base_fields(normalized, location=location)
    _require_supported_event_envelope(normalized, location=location)
    try:
        # Validate through Pydantic's JSON path so strict models still accept
        # JSON-native datetime strings while rejecting Python-side coercions.
        return SessionEntryAdapter.validate_json(json.dumps(normalized))
    except (TypeError, ValidationError) as exc:
        raise MalformedSessionEntryError(f"Malformed session entry{location}") from exc


def _require_persisted_base_fields(raw: JsonObject, *, location: str) -> None:
    """Prevent defaults from manufacturing unstable persisted identity or time."""

    missing = tuple(field for field in ("id", "session_id", "created_at") if field not in raw)
    if missing:
        fields = ", ".join(missing)
        raise MalformedSessionEntryError(
            f"Persisted session entry is missing required field(s) {fields}{location}"
        )


def _normalize_v2_structural_fields(
    raw: JsonObject,
    *,
    parent_id: str | None,
) -> JsonObject:
    """Restore null references omitted by public exclude-none serialization."""

    normalized = dict(raw)
    kind = raw.get("kind")
    if kind in {"message", "event", "compaction"}:
        normalized.setdefault("parent_id", parent_id)
    elif kind == "active_leaf":
        normalized.setdefault("previous_leaf_id", parent_id)
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
    version = envelope.payload.get("schema_version")
    if type(version) is not int:
        raise MalformedPersistedEventError(
            f"Persisted event schema_version must be an integer{location}"
        )
    if not _MIN_SUPPORTED_EVENT_SCHEMA_VERSION <= version <= EVENT_SCHEMA_VERSION:
        raise UnsupportedPersistedEventVersionError(
            f"Unsupported persisted event schema_version {version}{location}; expected "
            f"{_MIN_SUPPORTED_EVENT_SCHEMA_VERSION} through {EVENT_SCHEMA_VERSION}"
        )
    try:
        return wisp_event_from_dict(envelope.payload)
    except (ValidationError, ValueError) as exc:
        raise MalformedPersistedEventError(f"Malformed persisted event{location}") from exc


def _upgrade_v1_entry(
    raw: JsonObject,
    *,
    source: str | None,
    parent_id: str | None,
) -> JsonObject:
    """Upgrade one v1 discriminated entry into an in-memory current tree node."""

    location = f" at {source}" if source is not None else ""
    kind = raw.get("kind")
    if kind not in {"message", "event", "compaction"}:
        raise MalformedSessionEntryError(f"Unknown v1 session entry kind {kind!r}{location}")
    forbidden = tuple(
        field for field in ("parent_id", "previous_leaf_id", "active_leaf_id") if field in raw
    )
    if forbidden:
        fields = ", ".join(forbidden)
        raise MalformedSessionEntryError(
            f"V1 session entry contains v2 structural field(s) {fields}{location}"
        )
    normalized = dict(raw)
    normalized["schema_version"] = SESSION_ENTRY_SCHEMA_VERSION
    normalized["parent_id"] = parent_id
    return normalized


def _upgrade_legacy_entry(
    raw: JsonObject,
    *,
    source: str | None,
    parent_id: str | None,
) -> JsonObject:
    """Upgrade the unversioned flat entry shape used before schema v1."""

    location = f" at {source}" if source is not None else ""
    kind = raw.get("kind", "message")
    if kind not in {"message", "event", "compaction"}:
        raise MalformedSessionEntryError(f"Unknown legacy session entry kind {kind!r}{location}")
    populated = tuple(
        name for name in ("message", "event", "compaction") if raw.get(name) is not None
    )
    if populated != (kind,):
        raise MalformedSessionEntryError(
            f"Legacy {kind} session entries require exactly a {kind} payload{location}"
        )

    normalized: JsonObject = {
        "schema_version": SESSION_ENTRY_SCHEMA_VERSION,
        "kind": kind,
        "parent_id": parent_id,
    }
    for field in ("id", "session_id", "operation_id", "created_at"):
        if field in raw:
            normalized[field] = raw[field]

    if kind == "event":
        event = raw.get("event")
        if not isinstance(event, dict):
            raise MalformedSessionEntryError(
                f"Legacy event session entries require an event object{location}"
            )
        normalized["event"] = {
            "schema_version": PERSISTED_EVENT_ENVELOPE_SCHEMA_VERSION,
            "payload": event,
        }
    else:
        normalized[kind] = raw[kind]
    return normalized
