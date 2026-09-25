"""Build parent-linked session entries for tests."""

from __future__ import annotations

from collections.abc import Sequence

from wisp.sessions.entries import SessionEntry, is_session_tree_entry


def linked_entries(entries: Sequence[SessionEntry]) -> tuple[SessionEntry, ...]:
    """Chain tree entries into one linear branch in the given order.

    Args:
        entries (Sequence[SessionEntry]): Entries whose tree members have no parent yet.

    Returns:
        tuple[SessionEntry, ...]: The entries with each tree entry's ``parent_id`` set
        to the previous tree entry. Non-tree entries are returned unchanged.
    """

    parent_id: str | None = None
    linked: list[SessionEntry] = []
    for entry in entries:
        if is_session_tree_entry(entry):
            entry = entry.model_copy(update={"parent_id": parent_id})
            parent_id = entry.id
        linked.append(entry)
    return tuple(linked)
