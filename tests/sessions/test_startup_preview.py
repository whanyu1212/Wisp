from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO

import pytest

from wisp.events import ImagePart, SessionDirectoryChanged, ToolCallSnapshot
from wisp.messages import CompactionRecord, Message
from wisp.sessions import startup_preview
from wisp.sessions.entries import (
    ActiveLeafSessionEntry,
    CompactionSessionEntry,
    EventSessionEntry,
    MessageSessionEntry,
    PersistedEventEnvelope,
    SessionEntry,
    session_entry_to_json,
)
from wisp.sessions.pagination import DEFAULT_SESSION_MESSAGE_PAGE_LIMIT
from wisp.sessions.startup_preview import prepare_startup_preview, verify_startup_preview
from wisp.skills.models import SkillInvocationEvidence

_PAGE = DEFAULT_SESSION_MESSAGE_PAGE_LIMIT


def _message(i: int, message: Message | None = None) -> MessageSessionEntry:
    return MessageSessionEntry(
        id=f"m{i}",
        parent_id=f"m{i - 1}" if i else None,
        session_id="s",
        message=message or Message(role="assistant" if i % 2 else "user", content=f"message {i}"),
    )


def _append(path: Path, entries: Iterable[SessionEntry]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        for entry in entries:
            stream.write(session_entry_to_json(entry) + "\n")


@pytest.fixture
def history(tmp_path: Path) -> Path:
    path = tmp_path.resolve() / "history.jsonl"
    _append(path, (_message(i) for i in range(_PAGE + 1)))
    return path


def _packet(path: Path) -> bytes:
    packet = prepare_startup_preview(path, path.parent)
    assert packet is not None
    return packet


def _read(path: Path) -> tuple[str, ...]:
    excerpts = verify_startup_preview(_packet(path), path, "s", path.parent)
    assert excerpts is not None
    return excerpts


def _expected(last: int) -> tuple[str, ...]:
    return tuple(
        f"{'Assistant' if i % 2 else 'User'}: message {i}" for i in range(last - 7, last + 1)
    )


def test_latest_branch_after_compaction(history: Path) -> None:
    _append(
        history,
        [
            _message(_PAGE + 1, Message(role="assistant", content="abandoned")),
            ActiveLeafSessionEntry(
                session_id="s", previous_leaf_id=f"m{_PAGE + 1}", active_leaf_id=f"m{_PAGE}"
            ),
            MessageSessionEntry(
                id="reply",
                parent_id=f"m{_PAGE}",
                session_id="s",
                message=Message(role="assistant", content="active reply"),
            ),
            CompactionSessionEntry(
                parent_id="reply",
                session_id="s",
                compaction=CompactionRecord(
                    summary="provider summary",
                    provider="fake",
                    replaced_entry_ids=tuple(f"m{i}" for i in range(_PAGE)),
                ),
            ),
        ],
    )
    assert _read(history) == (*_expected(_PAGE)[1:], "Assistant: active reply")


def test_only_visible_conversation(history: Path) -> None:
    image = ImagePart(media_type="image/png", sha256="0" * 64, byte_size=32, width=1, height=1)
    messages = [
        Message(
            role="user",
            content="provider-expanded instructions",
            images=(image,),
            skill_invocation=SkillInvocationEvidence(
                name="test",
                original_content="\n/test\nmore\n",
                request="hidden",
                content_sha256="1" * 64,
            ),
        ),
        Message(
            role="assistant",
            content="checking\r\nprogress\x1b\u202e",
            tool_calls=(ToolCallSnapshot(call_id="id", name="read", arguments={"secret": 1}),),
        ),
        Message(role="tool", tool_call_id="id", tool_name="read", content="hidden tool result"),
        Message(role="assistant", content=" \t\n "),
        Message(role="user", content="", images=(image,)),
        Message(role="system", content="hidden system message"),
    ]
    _append(history, (_message(i, m) for i, m in enumerate(messages, start=_PAGE + 1)))
    assert _read(history) == (
        *_expected(_PAGE)[2:],
        "User: \n/test\nmore\n",
        "Assistant: checking\nprogress��",
    )


def test_clips_long_suffix(history: Path) -> None:
    suffix = "🦊" * 5000 + "\nlast line"
    _append(
        history,
        (
            _message(i, Message(role="user", content="start" + suffix))
            for i in range(_PAGE + 1, _PAGE + 9)
        ),
    )
    data = _packet(history)
    assert len(data) <= 512 * 1024
    excerpts = verify_startup_preview(data, history, "s", history.parent)
    assert excerpts is not None and len(excerpts) == 8
    for excerpt in excerpts:
        assert excerpt.startswith("User: …🦊") and excerpt.endswith("\nlast line")
        assert len(excerpt) == 4096


def test_requires_multiple_active_pages(history: Path) -> None:
    assert _read(history) == _expected(_PAGE)
    _append(
        history,
        (
            ActiveLeafSessionEntry(
                session_id="s",
                previous_leaf_id=f"m{_PAGE}",
                active_leaf_id=f"m{_PAGE - 1}",
            ),
        ),
    )
    assert prepare_startup_preview(history, history.parent) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 0),
        ("session_id", "foreign"),
        ("message", {"role": "tool"}),
        ("id", "m0"),
        ("parent_id", "missing"),
        ("kind", []),
    ],
)
def test_rejects_earlier_corruption(history: Path, field: str, value: object) -> None:
    rows = history.read_bytes().splitlines(keepends=True)
    entry = json.loads(rows[1])
    entry[field] = value
    rows[1] = json.dumps(entry).encode() + b"\n"
    history.write_bytes(b"".join(rows))
    assert prepare_startup_preview(history, history.parent) is None


def test_invalid_compaction_falls_back(history: Path) -> None:
    _append(
        history,
        (
            CompactionSessionEntry(
                parent_id=f"m{_PAGE}",
                session_id="s",
                compaction=CompactionRecord(
                    summary="bad prefix",
                    provider="fake",
                    replaced_entry_ids=("m3",),
                ),
            ),
        ),
    )
    assert prepare_startup_preview(history, history.parent) is None


def test_checks_recorded_directory(history: Path) -> None:
    foreign = history.parent / "other"
    foreign.mkdir()
    event = SessionDirectoryChanged(session_id="s", cwd=foreign)
    _append(
        history,
        (
            EventSessionEntry(
                session_id="s",
                parent_id=f"m{_PAGE}",
                event=PersistedEventEnvelope(payload=event.model_dump(mode="json")),
            ),
        ),
    )
    assert prepare_startup_preview(history, history.parent) is None
    data = prepare_startup_preview(history, foreign)
    assert data is not None
    assert verify_startup_preview(data, history, "s", history.parent) is None
    assert verify_startup_preview(data, history, "s", foreign) == _expected(_PAGE)


@pytest.mark.parametrize("change", ["rewrite", "swap", "append", "move", "symlink", "hardlink"])
def test_rejects_changed_selection(history: Path, change: str) -> None:
    data = _packet(history)
    info = history.stat()
    other = history.with_name("other")
    if change == "rewrite":
        history.write_bytes(history.read_bytes().replace(b"message 1", b"changed 1"))
        os.utime(history, ns=(info.st_atime_ns, info.st_mtime_ns))
    elif change == "swap":
        other.write_bytes(history.read_bytes())
        os.utime(other, ns=(info.st_atime_ns, info.st_mtime_ns))
        other.replace(history)
    elif change == "append":
        _append(history, [_message(_PAGE + 1)])
    elif change == "move":
        history = history.rename(other)
    elif change == "symlink":
        history.rename(other)
        history.symlink_to(other)
    else:
        other.hardlink_to(history)
    assert verify_startup_preview(data, history, "s", history.parent) is None


@pytest.mark.parametrize("kind", ["symlink", "parent_link", "hardlink", "fifo", "writable"])
def test_rejects_unsafe_files(history: Path, kind: str) -> None:
    other = history.with_name("alternative")
    if kind == "symlink":
        other.symlink_to(history)
    elif kind == "parent_link":
        other.symlink_to(history.parent, target_is_directory=True)
        other /= history.name
    elif kind == "hardlink":
        other.hardlink_to(history)
    elif kind == "fifo":
        os.mkfifo(other)
    else:
        other = history
        other.chmod(0o666)
    assert prepare_startup_preview(other, history.parent) is None


@pytest.mark.parametrize("tail", [b'{"schema', b"{}", b"\xff\n"])
def test_leaves_tail_untouched(history: Path, tail: bytes) -> None:
    original = history.read_bytes() + tail
    history.write_bytes(original)
    assert prepare_startup_preview(history, history.parent) is None
    assert history.read_bytes() == original
    assert list(history.parent.iterdir()) == [history]


def test_rejects_oversized_file(history: Path) -> None:
    with history.open("ab") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)
    assert prepare_startup_preview(history, history.parent) is None


def test_includes_exact_file_limit(history: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    size = history.stat().st_size
    monkeypatch.setattr(startup_preview, "_MAX_FILE_BYTES", size)
    data = _packet(history)
    assert verify_startup_preview(data, history, "s", history.parent) == _expected(_PAGE)
    monkeypatch.setattr(startup_preview, "_MAX_FILE_BYTES", size - 1)
    assert prepare_startup_preview(history, history.parent) is None
    assert verify_startup_preview(data, history, "s", history.parent) is None


@pytest.mark.parametrize("operation", ["prepare", "verify"])
def test_bounds_growing_read(
    history: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    packet = _packet(history)
    size = history.stat().st_size
    monkeypatch.setattr(startup_preview, "_MAX_FILE_BYTES", size)
    requests: list[int] = []

    class AppendingReader(io.FileIO):
        def read(self, size: int = -1) -> bytes:
            requests.append(size)
            if len(requests) == 1:
                _append(history, [_message(_PAGE + 1)])
            return super().read(size)

    def open_stream(fd: int, mode: str, buffering: int) -> BinaryIO:
        assert buffering == 0
        return AppendingReader(fd, mode)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fdopen", open_stream)
        if operation == "prepare":
            assert prepare_startup_preview(history, history.parent) is None
        else:
            assert verify_startup_preview(packet, history, "s", history.parent) is None
    assert requests and min(requests) > 0 and sum(requests) <= size
    assert history.stat().st_size > size


@pytest.mark.parametrize("bad", [b"", b"{", b"[]", b" " * (512 * 1024 + 1)])
def test_declines_invalid_packet(history: Path, bad: bytes) -> None:
    assert verify_startup_preview(bad, history, "s", history.parent) is None


def _worker(request: bytes) -> bytes:
    result = subprocess.run(
        [sys.executable, "-m", "wisp.sessions.startup_preview"],
        input=request,
        capture_output=True,
        check=True,
        timeout=15,
    )
    assert not result.stderr
    return result.stdout


def test_worker_pipe_roundtrip(history: Path) -> None:
    packet = _worker(json.dumps([str(history), str(history.parent)]).encode())
    assert verify_startup_preview(packet, history, "s", history.parent) == _expected(_PAGE)


@pytest.mark.parametrize("payload", [b'["relative","/"]', b'["/","/","/"]', b" " * 65537, b"{"])
def test_worker_declines_request(payload: bytes) -> None:
    assert _worker(payload) == b""
