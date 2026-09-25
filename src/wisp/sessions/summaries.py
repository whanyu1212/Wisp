"""Bounded session metadata scanning for session listings.

Listing sessions must not load or replay full transcripts. This module scans a
JSONL file once, tracking only the fields needed to report the current leaf,
entry count, and display name, while accepting exactly the entries the full
reader accepts.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from wisp.events import JsonObject
from wisp.sessions.entries import (
    MAX_SESSION_NAME_BYTES,
    PERSISTED_EVENT_ENVELOPE_SCHEMA_VERSION,
    SESSION_ENTRY_SCHEMA_VERSION,
    normalize_session_name,
)
from wisp.sessions.errors import (
    MalformedPersistedEventError,
    MalformedSessionEntryError,
    SessionError,
    SessionNotFoundError,
    UnsupportedPersistedEventVersionError,
    UnsupportedSessionEntryVersionError,
)
from wisp.sessions.file_io import interprocess_lock, recover_incomplete_tail, session_file_state
from wisp.sessions.replay import SessionReplayError


@dataclass(frozen=True, slots=True)
class _SessionSummaryEntryMetadata:
    id: str
    session_id: str
    kind: str
    parent_id: str | None = None
    previous_leaf_id: str | None = None
    active_leaf_id: str | None = None
    name: str | None = None
    message_role: str | None = None
    reason: str | None = None
    selected_entry_id: str | None = None
    source_transition_id: str | None = None


@dataclass(frozen=True, slots=True)
class SessionSummaryMetadata:
    session_id: str
    entry_count: int
    active_leaf_id: str | None
    name: str | None


_SESSION_TREE_ENTRY_KINDS = frozenset({"message", "event", "compaction"})


def read_session_summary_metadata(
    path: Path, *, check_cancelled: Callable[[], None] | None = None
) -> SessionSummaryMetadata:
    """Scan committed metadata under the existing recovery and locking boundary.

    Args:
        path (Path): Authorized session file to inspect.
        check_cancelled (Callable | None): Optional check between JSONL records.
            Exceptions propagate after releasing the file locks.

    Returns:
        SessionSummaryMetadata: Current identity, name, leaf, and entry count.

    Raises:
        SessionError: The file or committed metadata cannot be read safely.
    """
    state = session_file_state(path)
    with state.lock:
        with interprocess_lock(path, prepare_parent=False):
            if recover_incomplete_tail(path):
                state.generation += 1
            return _read_session_summary_metadata_unlocked(path, check_cancelled=check_cancelled)


def _read_session_summary_metadata_unlocked(
    path: Path, *, check_cancelled: Callable[[], None] | None = None
) -> SessionSummaryMetadata:
    if not path.is_file():
        raise SessionNotFoundError(f"Session file does not exist: {path}")

    session_id: str | None = None
    entry_count = 0
    active_leaf_id: str | None = None
    name: str | None = None
    node_ids: set[str] = set()
    node_metadata: dict[str, _SessionSummaryEntryMetadata] = {}
    transition_metadata: dict[str, _SessionSummaryEntryMetadata] = {}
    latest_history_entry_id: str | None = None
    seen_entry_ids: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as session_file:
            for line_number, line in enumerate(session_file, start=1):
                if check_cancelled is not None:
                    check_cancelled()
                if not line.strip():
                    continue
                source = f"{path}:{line_number}"
                entry = _summary_entry_metadata_from_json(line, source=source)
                if session_id is None:
                    session_id = entry.session_id
                elif entry.session_id != session_id:
                    raise MalformedSessionEntryError(
                        f"Session entry at {source} belongs to {entry.session_id}, "
                        f"expected {session_id}"
                    )
                if entry.id in seen_entry_ids:
                    raise MalformedSessionEntryError(
                        f"Duplicate session entry id {entry.id} at {source}"
                    )
                seen_entry_ids.add(entry.id)
                active_leaf_id, latest_history_entry_id = _resolve_summary_entry(
                    entry,
                    active_leaf_id=active_leaf_id,
                    node_ids=node_ids,
                    node_metadata=node_metadata,
                    transition_metadata=transition_metadata,
                    latest_history_entry_id=latest_history_entry_id,
                )
                if entry.kind == "session_info":
                    name = entry.name
                entry_count += 1
    except UnicodeDecodeError as exc:
        raise SessionError(f"Session file is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise SessionError(f"Could not read session file: {path}") from exc
    if session_id is None:
        raise SessionError(f"Session file is empty: {path}")
    return SessionSummaryMetadata(
        session_id=session_id,
        entry_count=entry_count,
        active_leaf_id=active_leaf_id,
        name=name,
    )


def _summary_entry_metadata_from_json(line: str, *, source: str) -> _SessionSummaryEntryMetadata:
    location = f" at {source}"
    try:
        raw_value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise MalformedSessionEntryError(f"Malformed session entry JSON{location}") from exc
    if not isinstance(raw_value, dict):
        raise MalformedSessionEntryError(f"Malformed session entry JSON{location}")
    raw = cast(JsonObject, raw_value)

    _require_summary_entry_version(raw, location=location)
    _require_summary_base_fields(raw, location=location)
    kind = raw.get("kind")
    entry_id = _required_summary_string(raw, "id", location=location)
    session_id = _required_summary_string(raw, "session_id", location=location)
    if kind in _SESSION_TREE_ENTRY_KINDS:
        _require_summary_declared_payload(raw, kind, location=location)
        if kind == "event":
            _require_summary_event_envelope(raw, location=location)
        return _SessionSummaryEntryMetadata(
            id=entry_id,
            session_id=session_id,
            kind=kind,
            parent_id=_optional_summary_string(raw, "parent_id", location=location),
            message_role=_summary_message_role(raw, kind),
        )
    if kind == "active_leaf":
        return _active_leaf_summary_entry_metadata(
            raw, entry_id=entry_id, session_id=session_id, location=location
        )
    if kind == "session_info":
        return _SessionSummaryEntryMetadata(
            id=entry_id,
            session_id=session_id,
            kind=kind,
            name=_summary_session_name(raw, location=location),
        )
    raise MalformedSessionEntryError(f"Malformed session entry{location}")


def _require_summary_entry_version(raw: JsonObject, *, location: str) -> None:
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


def _active_leaf_summary_entry_metadata(
    raw: JsonObject,
    *,
    entry_id: str,
    session_id: str,
    location: str,
) -> _SessionSummaryEntryMetadata:
    reason = raw.get("reason")
    selected_entry_id = raw.get("selected_entry_id")
    source_transition_id = raw.get("source_transition_id")
    if reason == "system":
        valid = selected_entry_id is None and source_transition_id is None
    elif reason == "navigation":
        valid = (
            isinstance(selected_entry_id, str)
            and bool(selected_entry_id)
            and source_transition_id is None
        )
    elif reason == "unrevert":
        valid = (
            isinstance(source_transition_id, str)
            and bool(source_transition_id)
            and selected_entry_id is None
        )
    else:
        valid = False
    if not valid:
        raise MalformedSessionEntryError(f"Malformed active-leaf transition metadata{location}")
    return _SessionSummaryEntryMetadata(
        id=entry_id,
        session_id=session_id,
        kind="active_leaf",
        previous_leaf_id=_optional_summary_string(raw, "previous_leaf_id", location=location),
        active_leaf_id=_optional_summary_string(raw, "active_leaf_id", location=location),
        reason=cast(str, reason),
        selected_entry_id=cast(str | None, selected_entry_id),
        source_transition_id=cast(str | None, source_transition_id),
    )


def _require_summary_base_fields(raw: JsonObject, *, location: str) -> None:
    missing = tuple(field for field in ("id", "session_id", "created_at") if field not in raw)
    if missing:
        fields = ", ".join(missing)
        raise MalformedSessionEntryError(
            f"Persisted session entry is missing required field(s) {fields}{location}"
        )


def _summary_message_role(raw: JsonObject, kind: object) -> str | None:
    if kind != "message":
        return None
    message = raw.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    return role if isinstance(role, str) else None


def _require_summary_event_envelope(raw: JsonObject, *, location: str) -> None:
    event = raw.get("event")
    if not isinstance(event, dict):
        raise MalformedSessionEntryError(f"Malformed session entry{location}")
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
    if not isinstance(event.get("payload"), dict):
        raise MalformedSessionEntryError(f"Malformed session entry{location}")


def _require_summary_declared_payload(raw: JsonObject, kind: object, *, location: str) -> None:
    assert isinstance(kind, str)
    if not isinstance(raw.get(kind), dict):
        raise MalformedSessionEntryError(f"Malformed session entry{location}")


def _required_summary_string(raw: JsonObject, field: str, *, location: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value:
        raise MalformedSessionEntryError(f"Malformed session entry{location}")
    return value


def _optional_summary_string(
    raw: JsonObject,
    field: str,
    *,
    location: str,
) -> str | None:
    value = raw.get(field)
    if value is None or isinstance(value, str):
        return value
    raise MalformedSessionEntryError(f"Malformed session entry{location}")


def _summary_session_name(raw: JsonObject, *, location: str) -> str | None:
    if "name" not in raw:
        raise MalformedSessionEntryError(f"Malformed session entry{location}")
    value = raw["name"]
    if value is None:
        return None
    if not isinstance(value, str):
        raise MalformedSessionEntryError(f"Malformed session entry{location}")
    normalized = normalize_session_name(value)
    if normalized is None:
        return None
    if len(normalized.encode("utf-8")) > MAX_SESSION_NAME_BYTES:
        raise MalformedSessionEntryError(
            f"session name cannot exceed {MAX_SESSION_NAME_BYTES} UTF-8 bytes{location}"
        )
    return normalized


def _resolve_summary_entry(
    entry: _SessionSummaryEntryMetadata,
    *,
    active_leaf_id: str | None,
    node_ids: set[str],
    node_metadata: dict[str, _SessionSummaryEntryMetadata],
    transition_metadata: dict[str, _SessionSummaryEntryMetadata],
    latest_history_entry_id: str | None,
) -> tuple[str | None, str | None]:
    if entry.kind in _SESSION_TREE_ENTRY_KINDS:
        parent_id = entry.parent_id
        if parent_id == entry.id:
            raise SessionReplayError(f"Session entry {entry.id} cannot parent itself")
        if parent_id is not None and parent_id not in node_ids:
            raise SessionReplayError(
                f"Session entry {entry.id} references unknown parent {parent_id}"
            )
        if parent_id != active_leaf_id:
            raise SessionReplayError(
                f"Session entry {entry.id} has parent {parent_id!r}, "
                f"expected active leaf {active_leaf_id!r}"
            )
        node_ids.add(entry.id)
        node_metadata[entry.id] = entry
        return entry.id, entry.id
    if entry.kind == "session_info":
        return active_leaf_id, latest_history_entry_id
    if entry.previous_leaf_id != active_leaf_id:
        raise SessionReplayError(
            f"Active-leaf entry {entry.id} expected previous leaf "
            f"{entry.previous_leaf_id!r}, found {active_leaf_id!r}"
        )
    if entry.active_leaf_id is not None and entry.active_leaf_id not in node_ids:
        raise SessionReplayError(
            f"Active-leaf entry {entry.id} references unknown leaf {entry.active_leaf_id}"
        )
    if entry.reason == "navigation":
        selected_entry_id = entry.selected_entry_id
        assert selected_entry_id is not None
        selected = node_metadata.get(selected_entry_id)
        if selected is None:
            raise SessionReplayError(
                f"Navigation entry {entry.id} references unknown selected entry {selected_entry_id}"
            )
        expected_leaf_id: str | None = selected.id
        if selected.kind == "message" and selected.message_role == "user":
            expected_leaf_id = selected.parent_id
        if entry.active_leaf_id != expected_leaf_id:
            raise SessionReplayError(
                f"Navigation entry {entry.id} selects {selected_entry_id} but activates "
                f"{entry.active_leaf_id!r}, expected {expected_leaf_id!r}"
            )
        if entry.active_leaf_id == entry.previous_leaf_id:
            raise SessionReplayError(f"Navigation entry {entry.id} records a no-op selection")
    elif entry.reason == "unrevert":
        source_transition_id = entry.source_transition_id
        assert source_transition_id is not None
        source = transition_metadata.get(source_transition_id)
        if source is None or source.reason != "navigation":
            raise SessionReplayError(
                f"Unrevert entry {entry.id} references invalid navigation transition "
                f"{source_transition_id}"
            )
        if latest_history_entry_id != source_transition_id:
            raise SessionReplayError(
                f"Unrevert entry {entry.id} does not reverse the latest history change"
            )
        if (
            entry.previous_leaf_id != source.active_leaf_id
            or entry.active_leaf_id != source.previous_leaf_id
        ):
            raise SessionReplayError(
                f"Unrevert entry {entry.id} is not the inverse of navigation {source_transition_id}"
            )
    transition_metadata[entry.id] = entry
    return entry.active_leaf_id, entry.id
