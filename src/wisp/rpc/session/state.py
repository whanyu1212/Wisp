"""Derived JSONL session snapshots shared by RPC command workers."""

from __future__ import annotations

from wisp.messages import Message
from wisp.rpc.coordinator import _RpcSessionState
from wisp.sessions.jsonl import JsonlSession


def rpc_session_state(session: JsonlSession | None) -> _RpcSessionState:
    if session is None or not session.path.is_file():
        return _RpcSessionState(session=session, history=(), entry_count=0)
    snapshot = session.read_run_snapshot()
    return _RpcSessionState(
        session=session,
        history=snapshot.replay.messages,
        entry_count=snapshot.entry_count,
        name=snapshot.name,
    )


def updated_rpc_session_state(
    session: JsonlSession,
    committed_history: tuple[Message, ...],
    entry_start: int,
) -> tuple[int, tuple[Message, ...]]:
    if not session.path.is_file():
        return entry_start, committed_history
    snapshot = session.read_existing_run_snapshot()
    return snapshot.entry_count, snapshot.replay.messages


def rpc_selected_session_state(
    session: JsonlSession,
) -> tuple[int, tuple[Message, ...], str | None, str | None]:
    snapshot = session.read_existing_run_snapshot()
    return snapshot.entry_count, snapshot.replay.messages, snapshot.active_leaf_id, snapshot.name


def rpc_derived_session_state(
    session: JsonlSession,
) -> tuple[int, tuple[Message, ...], str | None, str | None]:
    """Read a derived target, including a reserved empty first-message fork."""

    if not session.path.is_file():
        return 0, (), None, None
    return rpc_selected_session_state(session)
