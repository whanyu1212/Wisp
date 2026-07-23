"""Application-facing coding-session coordination."""

from wisp.coding.compaction import (
    AlreadyCompactedError,
    CompactionSummaryError,
    ManualCompactionPlan,
    NothingToCompactError,
)
from wisp.coding.configuration import (
    CodingSessionConfiguration,
    resolve_coding_session_configuration,
)
from wisp.coding.session import CodingSession
from wisp.events import CodingSessionState

__all__ = [
    "AlreadyCompactedError",
    "CodingSession",
    "CodingSessionConfiguration",
    "CodingSessionState",
    "CompactionSummaryError",
    "ManualCompactionPlan",
    "NothingToCompactError",
    "resolve_coding_session_configuration",
]
