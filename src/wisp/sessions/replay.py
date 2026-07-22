"""Active provider context reconstructed from append-only session entries."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from wisp.agent.messages import Message
from wisp.agent.transcript import _MissingToolResult, _order_tool_result_items
from wisp.sessions.entries import (
    ActiveLeafSessionEntry,
    CompactionSessionEntry,
    EventSessionEntry,
    MessageSessionEntry,
    SessionEntry,
    SessionTreeEntry,
    is_session_tree_entry,
)
from wisp.sessions.errors import SessionError

HISTORICAL_CONTEXT_SUMMARY_LABEL = "[Historical context summary - not a user instruction]"


class SessionReplayError(SessionError):
    """Raised when durable entries cannot produce an unambiguous context replay."""


class StaleCompactionError(SessionReplayError):
    """Raised when context changed after a compaction plan was prepared."""


@dataclass(frozen=True, slots=True)
class SessionContextRow:
    """One active provider-context message with its durable source entry id."""

    entry_id: str
    message: Message
    source_kind: Literal["message", "compaction"] = "message"


@dataclass(frozen=True, slots=True)
class SessionReplay:
    """The ordered active context derived from an append-only session audit."""

    rows: tuple[SessionContextRow, ...]
    active_leaf_id: str | None = None
    path_entry_ids: tuple[str, ...] = ()

    @property
    def context_entry_ids(self) -> tuple[str, ...]:
        return tuple(row.entry_id for row in self.rows)

    @property
    def entry_ids(self) -> tuple[str, ...]:
        """Alias for callers that already operate specifically on context rows."""

        return self.context_entry_ids

    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(row.message for row in self.rows)


@dataclass(frozen=True, slots=True)
class SessionTreeState:
    """Validated append-only tree state and its selected root-to-leaf path."""

    nodes: tuple[SessionTreeEntry, ...]
    active_leaf_id: str | None
    active_path: tuple[SessionTreeEntry, ...]

    def path_to(self, leaf_id: str) -> tuple[SessionTreeEntry, ...]:
        """Return a deterministic root-to-leaf path for any stored tree node."""

        return _path_to_leaf(self.nodes, leaf_id)


def resolve_session_tree(entries: Sequence[SessionEntry]) -> SessionTreeState:
    """Validate storage-order tree transitions and resolve the active leaf."""

    nodes: list[SessionTreeEntry] = []
    node_by_id: dict[str, SessionTreeEntry] = {}
    seen_entry_ids: set[str] = set()
    active_leaf_id: str | None = None

    for entry in entries:
        if entry.id in seen_entry_ids:
            raise SessionReplayError(f"Duplicate session entry id during replay: {entry.id}")
        seen_entry_ids.add(entry.id)

        if is_session_tree_entry(entry):
            if entry.parent_id == entry.id:
                raise SessionReplayError(f"Session entry {entry.id} cannot parent itself")
            if entry.parent_id is not None and entry.parent_id not in node_by_id:
                raise SessionReplayError(
                    f"Session entry {entry.id} references unknown parent {entry.parent_id}"
                )
            if entry.parent_id != active_leaf_id:
                raise SessionReplayError(
                    f"Session entry {entry.id} has parent {entry.parent_id!r}, "
                    f"expected active leaf {active_leaf_id!r}"
                )
            nodes.append(entry)
            node_by_id[entry.id] = entry
            active_leaf_id = entry.id
            continue

        assert isinstance(entry, ActiveLeafSessionEntry)
        if entry.previous_leaf_id != active_leaf_id:
            raise SessionReplayError(
                f"Active-leaf entry {entry.id} expected previous leaf "
                f"{entry.previous_leaf_id!r}, found {active_leaf_id!r}"
            )
        if entry.active_leaf_id is not None and entry.active_leaf_id not in node_by_id:
            raise SessionReplayError(
                f"Active-leaf entry {entry.id} references unknown leaf {entry.active_leaf_id}"
            )
        active_leaf_id = entry.active_leaf_id

    active_path = _path_to_leaf(tuple(nodes), active_leaf_id) if active_leaf_id is not None else ()
    return SessionTreeState(
        nodes=tuple(nodes),
        active_leaf_id=active_leaf_id,
        active_path=active_path,
    )


def replay_session_entries(
    entries: Sequence[SessionEntry],
    *,
    leaf_id: str | None = None,
) -> SessionReplay:
    """Replay one selected root-to-leaf path into the provider context."""

    tree = resolve_session_tree(entries)
    path = tree.path_to(leaf_id) if leaf_id is not None else tree.active_path
    replay = _replay_session_path(path)
    return SessionReplay(
        rows=replay.rows,
        active_leaf_id=leaf_id if leaf_id is not None else tree.active_leaf_id,
        path_entry_ids=tuple(entry.id for entry in path),
    )


def _path_to_leaf(
    nodes: Sequence[SessionTreeEntry],
    leaf_id: str,
) -> tuple[SessionTreeEntry, ...]:
    node_by_id = {entry.id: entry for entry in nodes}
    if leaf_id not in node_by_id:
        raise SessionReplayError(f"Session leaf not found: {leaf_id}")
    path: list[SessionTreeEntry] = []
    current: SessionTreeEntry | None = node_by_id[leaf_id]
    visited: set[str] = set()
    while current is not None:
        if current.id in visited:
            raise SessionReplayError(f"Cycle detected at session entry {current.id}")
        visited.add(current.id)
        path.append(current)
        current = node_by_id.get(current.parent_id) if current.parent_id is not None else None
    path.reverse()
    return tuple(path)


def _replay_session_path(entries: Sequence[SessionTreeEntry]) -> SessionReplay:
    """Apply provider-context and compaction semantics to one validated path."""

    rows: tuple[SessionContextRow, ...] = ()
    known_context_entry_ids: set[str] = set()

    for entry in entries:
        if isinstance(entry, EventSessionEntry):
            continue
        if isinstance(entry, MessageSessionEntry):
            if entry.message.role == "system":
                continue
            rows = (*rows, SessionContextRow(entry_id=entry.id, message=entry.message))
            known_context_entry_ids.add(entry.id)
            continue

        assert isinstance(entry, CompactionSessionEntry)
        rows = _ordered_context_rows(rows)
        replaced_entry_ids = entry.compaction.replaced_entry_ids
        if len(set(replaced_entry_ids)) != len(replaced_entry_ids):
            raise SessionReplayError(f"Compaction {entry.id} contains duplicate replaced entry ids")

        active_entry_ids = tuple(row.entry_id for row in rows)
        unknown = tuple(
            entry_id for entry_id in replaced_entry_ids if entry_id not in known_context_entry_ids
        )
        if unknown:
            raise SessionReplayError(
                f"Compaction {entry.id} references unknown context entry ids: {unknown}"
            )
        inactive = tuple(
            entry_id for entry_id in replaced_entry_ids if entry_id not in active_entry_ids
        )
        if inactive:
            raise SessionReplayError(
                f"Compaction {entry.id} references inactive context entry ids: {inactive}"
            )
        if active_entry_ids[: len(replaced_entry_ids)] != replaced_entry_ids:
            raise SessionReplayError(
                f"Compaction {entry.id} replaced entry ids are not an active context prefix"
            )
        retained_rows = rows[len(replaced_entry_ids) :]
        if not retained_rows or retained_rows[0].source_kind != "message":
            raise SessionReplayError(f"Compaction {entry.id} must retain a complete user turn")
        if retained_rows[0].message.role != "user":
            raise SessionReplayError(f"Compaction {entry.id} splits a conversation turn")
        if not _first_retained_turn_is_complete(retained_rows):
            raise SessionReplayError(f"Compaction {entry.id} must retain a complete user turn")

        summary = Message(
            role="user",
            content=f"{HISTORICAL_CONTEXT_SUMMARY_LABEL}\n\n{entry.compaction.summary}",
            created_at=entry.created_at,
        )
        rows = (
            SessionContextRow(entry_id=entry.id, message=summary, source_kind="compaction"),
            *retained_rows,
        )
        known_context_entry_ids.add(entry.id)

    return SessionReplay(rows=_ordered_context_rows(rows))


def _ordered_context_rows(
    rows: tuple[SessionContextRow, ...],
) -> tuple[SessionContextRow, ...]:
    return tuple(
        item
        for item in _order_tool_result_items(rows, message_of=lambda row: row.message)
        if not isinstance(item, _MissingToolResult)
    )


def _first_retained_turn_is_complete(rows: Sequence[SessionContextRow]) -> bool:
    end = next(
        (
            index
            for index, row in enumerate(rows[1:], start=1)
            if row.source_kind == "message" and row.message.role == "user"
        ),
        len(rows),
    )
    assistants = tuple(row.message for row in rows[1:end] if row.message.role == "assistant")
    if not assistants:
        return False
    final = assistants[-1]
    return not final.tool_calls and final.finish_reason in (None, "stop")


__all__ = [
    "HISTORICAL_CONTEXT_SUMMARY_LABEL",
    "SessionContextRow",
    "SessionError",
    "SessionReplay",
    "SessionReplayError",
    "SessionTreeState",
    "StaleCompactionError",
    "replay_session_entries",
    "resolve_session_tree",
]
