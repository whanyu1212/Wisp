"""Provider-neutral message vocabulary shared by providers, the agent runtime, and sessions.

The package sits below ``wisp.providers`` and ``wisp.agent``: providers know messages, the agent
knows providers. Import records from the package root; history helpers live in
``wisp.messages.history``.
"""

from .models import (
    CompactionRecord,
    Message,
    NativeOutput,
    Role,
    completion_event_has_history,
    message_from_completion_event,
)

__all__ = [
    "CompactionRecord",
    "Message",
    "NativeOutput",
    "Role",
    "completion_event_has_history",
    "message_from_completion_event",
]
