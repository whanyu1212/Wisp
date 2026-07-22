"""Session persistence package."""

from .entries import (
    PERSISTED_EVENT_ENVELOPE_SCHEMA_VERSION,
    SESSION_ENTRY_SCHEMA_VERSION,
    ActiveLeafSessionEntry,
    CompactionSessionEntry,
    EventSessionEntry,
    MessageSessionEntry,
    PersistedEventEnvelope,
    SessionEntry,
    SessionTreeEntry,
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
    SessionTreeState,
    StaleCompactionError,
    replay_session_entries,
    resolve_session_tree,
)

__all__ = [
    "JsonlSession",
    "JsonlSessionStore",
    "PERSISTED_EVENT_ENVELOPE_SCHEMA_VERSION",
    "SESSION_ENTRY_SCHEMA_VERSION",
    "ActiveLeafSessionEntry",
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
    "SessionTreeEntry",
    "SessionTreeState",
    "StaleCompactionError",
    "UnsupportedPersistedEventVersionError",
    "UnsupportedSessionEntryVersionError",
    "replay_session_entries",
    "resolve_session_tree",
]
