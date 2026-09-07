"""Derived JSONL session snapshots shared by RPC command workers."""

from __future__ import annotations

from wisp.agent.messages import Message
from wisp.rpc.coordinator import _RpcSessionState
from wisp.sessions.entries import SessionEntry, SessionInfoSessionEntry
from wisp.sessions.jsonl import JsonlSession
from wisp.sessions.replay import replay_session_entries


def rpc_session_state(session: JsonlSession | None) -> _RpcSessionState:
    if session is None or not session.path.is_file():
        return _RpcSessionState(session=session, history=(), entry_count=0)
    return _RpcSessionState(
        session=session,
        history=session.read_context_messages(),
        entry_count=len(session.read_entries()),
        name=session.read_name(),
    )


def updated_rpc_session_state(
    session: JsonlSession,
    committed_history: tuple[Message, ...],
    entry_start: int,
) -> tuple[int, tuple[Message, ...]]:
    if not session.path.is_file():
        return entry_start, committed_history
    entries = session.read_entry_snapshot()
    replay = replay_session_entries(entries)
    return len(entries), replay.messages


def rpc_selected_session_state(
    session: JsonlSession,
) -> tuple[int, tuple[Message, ...], str | None, str | None]:
    entries = session.read_entries()
    replay = replay_session_entries(entries)
    return len(entries), replay.messages, replay.active_leaf_id, _session_name_from_entries(entries)


def _session_name_from_entries(entries: tuple[SessionEntry, ...]) -> str | None:
    name: str | None = None
    for entry in entries:
        if isinstance(entry, SessionInfoSessionEntry):
            name = entry.name
    return name


def rpc_derived_session_state(
    session: JsonlSession,
) -> tuple[int, tuple[Message, ...], str | None, str | None]:
    """Read a derived target, including a reserved empty first-message fork."""

    if not session.path.is_file():
        return 0, (), None, None
    return rpc_selected_session_state(session)
