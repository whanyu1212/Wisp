"""Pure projections used by durable session branch operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from wisp.sessions.entries import MessageSessionEntry, SessionEntry, SessionTreeEntry
from wisp.sessions.errors import InvalidSessionBranchPointError
from wisp.sessions.replay import replay_session_entries, resolve_session_tree


@dataclass(frozen=True, slots=True)
class SessionBranchProjection:
    """A validated source path ready to be copied into a new session."""

    entries: tuple[SessionTreeEntry, ...]
    source_leaf_id: str | None
    mode: Literal["clone", "fork"]
    selected_entry_id: str | None = None
    selected_prompt: str | None = None


def project_session_path(
    entries: tuple[SessionEntry, ...],
    *,
    leaf_id: str | None,
    mode: Literal["clone", "fork"] = "clone",
) -> SessionBranchProjection:
    """Project one validated root-to-leaf path without mutating the source."""

    tree = resolve_session_tree(entries)
    if leaf_id is None:
        path: tuple[SessionTreeEntry, ...] = ()
    else:
        path = tree.path_to(leaf_id)
        # Structural tree validation is not sufficient for compactions. Ensure
        # the selected path can actually be resumed before creating a target.
        replay_session_entries(entries, leaf_id=leaf_id)
    return SessionBranchProjection(
        entries=path,
        source_leaf_id=leaf_id,
        mode=mode,
    )


def project_fork_from_user_message(
    entries: tuple[SessionEntry, ...],
    *,
    entry_id: str,
) -> SessionBranchProjection:
    """Project history before one user message and retain its editable text."""

    tree = resolve_session_tree(entries)
    selected = next((entry for entry in tree.nodes if entry.id == entry_id), None)
    if not isinstance(selected, MessageSessionEntry) or selected.message.role != "user":
        raise InvalidSessionBranchPointError(
            f"Session fork entry must be a persisted user message: {entry_id}"
        )

    projection = project_session_path(
        entries,
        leaf_id=selected.parent_id,
        mode="fork",
    )
    return SessionBranchProjection(
        entries=projection.entries,
        source_leaf_id=projection.source_leaf_id,
        mode="fork",
        selected_entry_id=selected.id,
        selected_prompt=selected.message.content,
    )
