"""Session persistence package."""

from .entries import (
    PERSISTED_EVENT_ENVELOPE_SCHEMA_VERSION,
    SESSION_ENTRY_SCHEMA_VERSION,
    CompactionSessionEntry,
    EventSessionEntry,
    MessageSessionEntry,
    PersistedEventEnvelope,
    SessionEntry,
)
from .errors import (
    MalformedPersistedEventError,
    MalformedSessionEntryError,
    SessionError,
    UnsupportedPersistedEventVersionError,
    UnsupportedSessionEntryVersionError,
)
from .jsonl import JsonlSession, JsonlSessionStore
from .replay import (
    SessionContextRow,
    SessionReplay,
    SessionReplayError,
    StaleCompactionError,
    replay_session_entries,
)

__all__ = [
    "JsonlSession",
    "JsonlSessionStore",
    "PERSISTED_EVENT_ENVELOPE_SCHEMA_VERSION",
    "SESSION_ENTRY_SCHEMA_VERSION",
    "CompactionSessionEntry",
    "EventSessionEntry",
    "MalformedPersistedEventError",
    "MalformedSessionEntryError",
    "MessageSessionEntry",
    "PersistedEventEnvelope",
    "SessionContextRow",
    "SessionEntry",
    "SessionError",
    "SessionReplay",
    "SessionReplayError",
    "StaleCompactionError",
    "UnsupportedPersistedEventVersionError",
    "UnsupportedSessionEntryVersionError",
    "replay_session_entries",
]
