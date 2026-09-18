"""Branch-pinned session writes for one coding run."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from wisp.agent.messages import Message
from wisp.events import WispEvent
from wisp.sessions.entries import (
    CompactionSessionEntry,
    EventSessionEntry,
    PersistedEventEnvelope,
    SessionEntry,
    is_session_tree_entry,
)
from wisp.sessions.jsonl import JsonlSession


@dataclass(slots=True)
class RunPersistence:
    """Advance one run only along the session branch it has observed."""

    session: JsonlSession
    expected_active_leaf_id: str | None
    operation_id: str | None

    async def append_entry(self, entry: SessionEntry) -> SessionEntry:
        persisted = await self.session.append_entry_if_current(
            entry,
            expected_active_leaf_id=self.expected_active_leaf_id,
        )
        if is_session_tree_entry(persisted):
            self.expected_active_leaf_id = persisted.id
        return persisted

    async def append_message(self, message: Message) -> SessionEntry:
        persisted = await self.session.append_message_if_current(
            message,
            expected_active_leaf_id=self.expected_active_leaf_id,
            operation_id=self.operation_id,
        )
        self.expected_active_leaf_id = persisted.id
        return persisted

    async def append_event(self, event: WispEvent) -> SessionEntry:
        persisted = await self.append_entry(
            EventSessionEntry(
                session_id=self.session.session_id,
                event=PersistedEventEnvelope(payload=event.model_dump(mode="json")),
                operation_id=self.operation_id,
            )
        )
        return persisted

    async def append_compaction(
        self,
        entry: CompactionSessionEntry,
        *,
        expected_context_entry_ids: Sequence[str],
    ) -> SessionEntry:
        persisted = await self.session.append_compaction_entry_if_current(
            entry,
            expected_context_entry_ids=expected_context_entry_ids,
            expected_active_leaf_id=self.expected_active_leaf_id,
        )
        self.expected_active_leaf_id = persisted.id
        return persisted


@dataclass(frozen=True, slots=True)
class PendingSessionEntry:
    """An entry queued for durable write through the run that produced it."""

    persistence: RunPersistence
    entry: SessionEntry
