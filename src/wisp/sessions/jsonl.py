"""Append-only JSONL session persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import anyio

from wisp.agent.messages import Message, SessionEntry


class JsonlSessionStore:
    """Creates JSONL-backed Wisp sessions."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def create(self) -> JsonlSession:
        session_id = uuid4().hex
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        path = self.root / f"{timestamp}-{session_id[:8]}.jsonl"
        return JsonlSession(session_id=session_id, path=path)


class JsonlSession:
    """A single append-only JSONL session file."""

    def __init__(self, *, session_id: str, path: Path) -> None:
        self.session_id = session_id
        self.path = path

    async def append_message(self, message: Message) -> SessionEntry:
        entry = SessionEntry(session_id=self.session_id, message=message)
        line = entry.model_dump_json()
        await anyio.to_thread.run_sync(self._append_line, line)
        return entry

    def _append_line(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as session_file:
            session_file.write(line)
            session_file.write("\n")
