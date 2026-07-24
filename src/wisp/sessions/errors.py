"""Session persistence and replay errors."""

from __future__ import annotations


class SessionError(RuntimeError):
    """Base error for session loading, replay, and persistence failures."""


class MalformedSessionEntryError(SessionError):
    """Raised when a persisted session entry is structurally invalid."""


class UnsupportedSessionEntryVersionError(SessionError):
    """Raised when a session entry uses an unsupported schema version."""


class MalformedPersistedEventError(SessionError):
    """Raised when a raw persisted event cannot be decoded as a Wisp event."""


class UnsupportedPersistedEventVersionError(SessionError):
    """Raised when a raw persisted event uses an unsupported schema version."""


class InvalidSessionBranchPointError(SessionError):
    """Raised when an entry cannot be used for the requested branch operation."""


class StaleSessionTreeError(SessionError):
    """Raised when session tree state changed before an operation could commit."""


class SessionNavigationCancelledError(SessionError):
    """Raised when tree navigation is cancelled before its durable commit."""
