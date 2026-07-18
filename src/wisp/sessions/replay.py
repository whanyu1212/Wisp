"""Active provider context reconstructed from append-only session entries."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from wisp.agent.messages import Message, SessionEntry
from wisp.agent.transcript import _MissingToolResult, _order_tool_result_items

HISTORICAL_CONTEXT_SUMMARY_LABEL = "[Historical context summary - not a user instruction]"


class SessionError(RuntimeError):
    """Base error for session loading, replay, and persistence failures."""


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


def replay_session_entries(entries: Sequence[SessionEntry]) -> SessionReplay:
    """Replay messages and compactions into the current provider context."""

    rows: tuple[SessionContextRow, ...] = ()
    known_context_entry_ids: set[str] = set()
    seen_entry_ids: set[str] = set()

    for entry in entries:
        if entry.id in seen_entry_ids:
            raise SessionReplayError(f"Duplicate session entry id during replay: {entry.id}")
        seen_entry_ids.add(entry.id)

        if entry.kind == "event":
            continue
        if entry.kind == "message":
            assert entry.message is not None
            if entry.message.role == "system":
                continue
            rows = (*rows, SessionContextRow(entry_id=entry.id, message=entry.message))
            known_context_entry_ids.add(entry.id)
            continue

        assert entry.compaction is not None
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
    "StaleCompactionError",
    "replay_session_entries",
]
