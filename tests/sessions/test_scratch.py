"""Each session's private scratch folder beside the session files."""

from __future__ import annotations

import stat
from pathlib import Path

import anyio
import pytest

from wisp.sessions.errors import SessionError
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore


def test_folder_beside_sessions(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path / "sessions").create()

    folder = anyio.run(session.ensure_scratch_directory)

    assert folder == (tmp_path / "sessions" / "scratch" / session.session_id).resolve()
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert anyio.run(session.ensure_scratch_directory) == folder


@pytest.mark.parametrize(
    "session_id",
    ["../escape", "..", "a/b", "/absolute", ".hidden", "x" * 129],
    ids=["dotdot-prefix", "dotdot", "nested", "absolute", "dot-leading", "too-long"],
)
def test_unsafe_id_refused(tmp_path: Path, session_id: str) -> None:
    """An id read from a session file cannot place the folder outside scratch/."""

    (tmp_path / "sessions").mkdir()
    outside = tmp_path / "absolute"
    outside.mkdir(mode=0o755)
    if session_id == "/absolute":
        session_id = str(outside)
    session = JsonlSession(session_id=session_id, path=tmp_path / "sessions" / "s.jsonl")

    with pytest.raises(SessionError, match="cannot name a scratch folder"):
        anyio.run(session.ensure_scratch_directory)

    assert sorted(path.name for path in tmp_path.iterdir()) == ["absolute", "sessions"]
    assert not (tmp_path / "sessions" / "scratch").exists()
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755


def test_linked_scratch_refused(tmp_path: Path) -> None:
    """A link planted at scratch/ cannot move every session's folder elsewhere."""

    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "sessions").mkdir(exist_ok=True)
    (tmp_path / "sessions" / "scratch").symlink_to(elsewhere)

    with pytest.raises(SessionError, match="not a directory"):
        anyio.run(session.ensure_scratch_directory)

    assert list(elsewhere.iterdir()) == []
