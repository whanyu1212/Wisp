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
    InvalidSessionBranchPointError,
    MalformedPersistedEventError,
    MalformedSessionEntryError,
    SessionError,
    StaleSessionTreeError,
    UnsupportedPersistedEventVersionError,
    UnsupportedSessionEntryVersionError,
)
from .jsonl import JsonlSession, JsonlSessionStore, SessionForkResult, SessionSummary
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
    "InvalidSessionBranchPointError",
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
    "SessionForkResult",
    "SessionReplay",
    "SessionReplayError",
    "SessionSummary",
    "SessionTreeEntry",
    "SessionTreeState",
    "StaleCompactionError",
    "StaleSessionTreeError",
    "UnsupportedPersistedEventVersionError",
    "UnsupportedSessionEntryVersionError",
    "replay_session_entries",
    "resolve_session_tree",
]
