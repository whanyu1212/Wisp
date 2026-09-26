from __future__ import annotations

import errno
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from wisp.agent.messages import Message
from wisp.sessions import (
    file_io,
)
from wisp.sessions import (
    jsonl as jsonl_module,
)
from wisp.sessions.entries import (
    MessageSessionEntry,
    SessionEntry,
    session_entry_to_json,
)
from wisp.sessions.errors import (
    MalformedSessionEntryError,
    StaleSessionWriterError,
)
from wisp.sessions.jsonl import (
    JsonlSession,
    JsonlSessionStore,
    SessionError,
    SessionNotFoundError,
)


@pytest.mark.parametrize(
    "incomplete_tail",
    [
        b'{"kind":"message"',
        b'{"valid":"json"}',
        b"\xf0\x9f\x99",
    ],
    ids=["partial-json", "valid-json-without-newline", "partial-utf8"],
)
def test_recovers_unterminated_final_bytes(
    tmp_path: Path,
    incomplete_tail: bytes,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    committed = MessageSessionEntry(
        id="committed",
        session_id=session.session_id,
        message=Message(role="user", content="safe"),
    )
    committed_bytes = f"{session_entry_to_json(committed)}\n".encode()
    session.path.write_bytes(committed_bytes + incomplete_tail)

    assert session.read_entries() == (committed,)
    assert session.path.read_bytes() == committed_bytes


def test_recovery_fills_short_reads_before_scanning_back(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    committed = MessageSessionEntry(
        id="committed",
        session_id=session.session_id,
        message=Message(role="user", content="safe"),
    )
    committed_bytes = f"{session_entry_to_json(committed)}\n".encode()
    session.path.write_bytes(committed_bytes + b"x" * (64 * 1024 + 1))
    real_read = os.read

    def short_read(fd: int, size: int) -> bytes:
        return real_read(fd, min(size, 64))

    monkeypatch.setattr(os, "read", short_read)

    assert session.read_entries() == (committed,)
    assert session.path.read_bytes() == committed_bytes


def test_removes_file_with_only_uncommitted_bytes(tmp_path: Path) -> None:
    path = tmp_path / "incomplete-only.jsonl"
    path.write_bytes(b'{"valid":"json"}')
    store = JsonlSessionStore(tmp_path)

    assert store.summaries() == ()
    assert not path.exists()
    with pytest.raises(SessionNotFoundError):
        store.load(path)


def test_preserves_committed_malformed_final_record(tmp_path: Path) -> None:
    path = tmp_path / "invalid.jsonl"
    malformed = b'{"kind":"message"\n'
    path.write_bytes(malformed)

    with pytest.raises(MalformedSessionEntryError, match="Malformed session entry JSON"):
        JsonlSessionStore(tmp_path).load(path)

    assert path.read_bytes() == malformed


def test_append_entry_is_idempotent(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()
    entry = MessageSessionEntry(
        id="entry-1",
        session_id=session.session_id,
        message=Message(role="assistant", content="done"),
    )

    async def write() -> None:
        assert await session.append_entry(entry) == entry
        assert await session.append_entry(entry) == entry

    anyio.run(write)

    assert session.read_entries() == (entry,)
    assert len(session.path.read_text(encoding="utf-8").splitlines()) == 1


def test_append_rechecks_identity_after_another_handle_writes(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    seed = MessageSessionEntry(
        id="seed-entry",
        session_id=session.session_id,
        message=Message(role="user", content="start"),
    )
    entry = MessageSessionEntry(
        id="entry-1",
        session_id=session.session_id,
        message=Message(role="assistant", content="done"),
    )

    async def write() -> None:
        await session.append_entry(seed)
        reopened = store.load(session.path)
        await reopened.append_entry(seed)
        await session.append_entry(entry)
        await reopened.append_entry(entry)

    anyio.run(write)

    assert session.read_entries() == (
        seed,
        entry.model_copy(update={"parent_id": seed.id}),
    )


def test_append_entry_rejects_conflicting_explicit_parent(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()
    seed = MessageSessionEntry(
        id="seed",
        session_id=session.session_id,
        message=Message(role="user", content="start"),
    )
    conflict = MessageSessionEntry(
        id="conflict",
        session_id=session.session_id,
        parent_id="other",
        message=Message(role="assistant", content="done"),
    )

    async def write() -> None:
        await session.append_entry(seed)
        with pytest.raises(SessionError, match="specifies parent 'other'"):
            await session.append_entry(conflict)

    anyio.run(write)

    assert session.read_entries() == (seed,)


def test_append_retry_rejects_same_id_with_conflicting_parent(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()
    seed = MessageSessionEntry(
        id="seed",
        session_id=session.session_id,
        message=Message(role="user", content="start"),
    )
    entry = MessageSessionEntry(
        id="entry",
        session_id=session.session_id,
        message=Message(role="assistant", content="done"),
    )

    async def write() -> None:
        await session.append_entry(seed)
        await session.append_entry(entry)
        with pytest.raises(SessionError, match="conflicts with persisted data"):
            await session.append_entry(entry.model_copy(update={"parent_id": "other"}))

    anyio.run(write)


def test_concurrent_append_entry_writes_one_record(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()
    entry = MessageSessionEntry(
        id="entry-1",
        session_id=session.session_id,
        message=Message(role="assistant", content="done"),
    )

    async def write() -> None:
        async with anyio.create_task_group() as task_group:
            for _ in range(8):
                task_group.start_soon(session.append_entry, entry)

    anyio.run(write)

    assert session.read_entries() == (entry,)


def test_concurrent_session_handles_append_one_record(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    seed = MessageSessionEntry(
        id="seed-entry",
        session_id=session.session_id,
        message=Message(role="user", content="start"),
    )
    entry = MessageSessionEntry(
        id="entry-1",
        session_id=session.session_id,
        message=Message(role="assistant", content="done"),
    )
    load_entry_index = JsonlSession._load_entry_index  # noqa: SLF001

    def delayed_load_entry_index(handle: JsonlSession) -> dict[str, SessionEntry]:
        index = load_entry_index(handle)
        time.sleep(0.02)
        return index

    monkeypatch.setattr(JsonlSession, "_load_entry_index", delayed_load_entry_index)

    async def write() -> None:
        await session.append_entry(seed)
        handles = [store.load(session.path) for _ in range(8)]
        async with anyio.create_task_group() as task_group:
            for handle in handles:
                task_group.start_soon(handle.append_entry, entry)

    anyio.run(write)

    assert session.read_entries() == (
        seed,
        entry.model_copy(update={"parent_id": seed.id}),
    )


@pytest.mark.production_fault
def test_conditional_append_rejects_stale_session_handle(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def write() -> None:
        seed = await session.append_message(Message(role="user", content="seed"))
        left = MessageSessionEntry(
            session_id=session.session_id,
            message=Message(role="assistant", content="left"),
        )
        right = MessageSessionEntry(
            session_id=session.session_id,
            message=Message(role="assistant", content="right"),
        )
        first = store.load(session.path)
        second = store.load(session.path)
        await first.append_entry_if_current(left, expected_active_leaf_id=seed.id)
        with pytest.raises(StaleSessionWriterError, match="expected active leaf"):
            await second.append_entry_if_current(right, expected_active_leaf_id=seed.id)

    anyio.run(write)

    assert [message.content for message in session.read_messages()] == ["seed", "left"]


@pytest.mark.production_fault
def test_conditional_append_reconciles_uncertain_stable_id_retry(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> tuple[SessionEntry, SessionEntry]:
        seed = await session.append_message(Message(role="user", content="seed"))
        entry = MessageSessionEntry(
            id="stable-answer",
            session_id=session.session_id,
            message=Message(role="assistant", content="done"),
        )
        first = await session.append_entry_if_current(
            entry,
            expected_active_leaf_id=seed.id,
        )
        retried = await session.append_entry_if_current(
            entry,
            expected_active_leaf_id=seed.id,
        )
        return first, retried

    first, retried = anyio.run(write)

    assert first == retried
    assert len(session.read_entries()) == 2


def test_repeated_append_does_not_rescan_full_session_tree(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    resolve_session_tree = jsonl_module.resolve_session_tree
    resolver_calls = 0

    def counted_resolver(entries: tuple[SessionEntry, ...]) -> object:
        nonlocal resolver_calls
        resolver_calls += 1
        return resolve_session_tree(entries)

    monkeypatch.setattr(jsonl_module, "resolve_session_tree", counted_resolver)

    async def write() -> None:
        for index in range(50):
            await session.append_message(Message(role="user", content=str(index)))

    anyio.run(write)

    assert resolver_calls == 0
    # Appends keep the entry index current, so reading back never re-resolves the
    # tree. Reads are served from that index rather than re-parsing the file.
    assert len(session.read_entries()) == 50
    assert resolver_calls == 0


def test_repeated_reads_parse_a_resumed_session_file_once(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """Resuming must not re-parse the transcript for every derived read.

    ``rpc_session_state`` asks a freshly loaded session for its context, its
    entry count and its name. Parsing the file once per call dominated startup
    on long sessions, so all three must share one pass over the entry index.
    """

    session = JsonlSessionStore(tmp_path).create()

    async def write() -> None:
        for index in range(20):
            await session.append_message(Message(role="user", content=str(index)))

    anyio.run(write)

    reopened = JsonlSessionStore(tmp_path).load(session.path)
    parses = 0
    read_entries_unlocked = jsonl_module._read_entries_unlocked

    def counted_parse(path: Path, **kwargs: object) -> list[SessionEntry]:
        nonlocal parses
        parses += 1
        return read_entries_unlocked(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(jsonl_module, "_read_entries_unlocked", counted_parse)

    assert len(reopened.read_entries()) == 20
    assert len(reopened.read_context_messages()) == 20
    assert reopened.read_name() is None

    assert parses == 1


def test_append_reloads_identity_after_uncertain_write(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    entry = MessageSessionEntry(
        id="entry-1",
        session_id=session.session_id,
        message=Message(role="assistant", content="done"),
    )
    append_line = session._append_line  # noqa: SLF001
    should_fail = True

    def append_then_fail(line: str) -> None:
        nonlocal should_fail
        append_line(line)
        if should_fail:
            should_fail = False
            raise OSError("uncertain write outcome")

    monkeypatch.setattr(session, "_append_line", append_then_fail)

    async def write() -> None:
        with pytest.raises(OSError, match="uncertain write outcome"):
            await session.append_entry(entry)
        await session.append_entry(entry)

    anyio.run(write)

    assert session.read_entries() == (entry,)


def test_write_all_retries_short_writes(monkeypatch: MonkeyPatch) -> None:
    written = bytearray()

    def short_write(_fd: int, data: bytes | memoryview) -> int:
        chunk = bytes(data[:2])
        written.extend(chunk)
        return len(chunk)

    monkeypatch.setattr(os, "write", short_write)

    file_io.write_all(123, b"abcdef")  # noqa: SLF001

    assert written == b"abcdef"


def test_write_all_rejects_zero_progress(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(os, "write", lambda _fd, _data: 0)

    with pytest.raises(OSError, match="made no progress"):
        file_io.write_all(123, b"record")  # noqa: SLF001


def test_failed_first_append_removes_empty_crash_artifact(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    monkeypatch.setattr(os, "write", lambda _fd, _data: 0)

    with pytest.raises(OSError, match="made no progress"):
        session._append_line('{"incomplete":true}')  # noqa: SLF001

    assert not session.path.exists()


@pytest.mark.production_fault
def test_append_failure_rolls_back_partial_record(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    committed = MessageSessionEntry(
        id="committed",
        session_id=session.session_id,
        message=Message(role="user", content="safe"),
    )
    session.path.write_bytes(f"{session_entry_to_json(committed)}\n".encode())
    original = session.path.read_bytes()
    real_write = os.write
    write_calls = 0

    def fail_after_short_write(fd: int, data: bytes | memoryview) -> int:
        nonlocal write_calls
        write_calls += 1
        if write_calls == 1:
            return real_write(fd, data[:7])
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(os, "write", fail_after_short_write)

    with pytest.raises(OSError, match="disk full"):
        session._append_line('{"incomplete":true}')  # noqa: SLF001

    assert session.path.read_bytes() == original
    assert session.read_entries() == (committed,)


@pytest.mark.production_fault
def test_append_sync_failure_rolls_back_complete_record(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    committed = MessageSessionEntry(
        id="committed",
        session_id=session.session_id,
        message=Message(role="user", content="safe"),
    )
    session.path.write_bytes(f"{session_entry_to_json(committed)}\n".encode())
    original = session.path.read_bytes()
    real_sync = jsonl_module.sync_file  # noqa: SLF001
    sync_calls = 0

    def fail_first_sync(fd: int) -> None:
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 1:
            raise OSError(errno.EIO, "sync failed")
        real_sync(fd)

    monkeypatch.setattr(jsonl_module, "sync_file", fail_first_sync)

    with pytest.raises(OSError, match="sync failed"):
        session._append_line('{"complete":true}')  # noqa: SLF001

    assert sync_calls == 2
    assert session.path.read_bytes() == original


def test_append_reports_uncertain_outcome_when_rollback_fails(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    committed = MessageSessionEntry(
        id="committed",
        session_id=session.session_id,
        message=Message(role="user", content="safe"),
    )
    session.path.write_bytes(f"{session_entry_to_json(committed)}\n".encode())
    real_write = os.write
    write_calls = 0

    def fail_after_short_write(fd: int, data: bytes | memoryview) -> int:
        nonlocal write_calls
        write_calls += 1
        if write_calls == 1:
            return real_write(fd, data[:7])
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(os, "write", fail_after_short_write)
    real_ftruncate = os.ftruncate

    def fail_ftruncate(_fd: int, _size: int) -> None:
        raise OSError(errno.EIO, "rollback failed")

    with monkeypatch.context() as rollback_failure:
        rollback_failure.setattr(os, "ftruncate", fail_ftruncate)
        with pytest.raises(SessionError, match="rollback could not be synchronized"):
            session._append_line('{"incomplete":true}')  # noqa: SLF001

    monkeypatch.setattr(os, "ftruncate", real_ftruncate)
    # The next cooperating read removes the uncommitted suffix.
    assert session.read_entries() == (committed,)
    assert session.path.read_bytes().endswith(b"\n")


def test_successful_append_syncs_file_and_new_parent_entry(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    file_syncs: list[int] = []
    directory_syncs: list[Path] = []
    real_file_sync = jsonl_module.sync_file  # noqa: SLF001
    real_directory_sync = jsonl_module.sync_directory  # noqa: SLF001

    def track_file_sync(fd: int) -> None:
        file_syncs.append(fd)
        real_file_sync(fd)

    def track_directory_sync(path: Path) -> None:
        directory_syncs.append(path)
        real_directory_sync(path)

    monkeypatch.setattr(jsonl_module, "sync_file", track_file_sync)
    monkeypatch.setattr(jsonl_module, "sync_directory", track_directory_sync)

    session._append_line('{"first":true}')  # noqa: SLF001
    session._append_line('{"second":true}')  # noqa: SLF001

    assert len(file_syncs) == 2
    assert directory_syncs == [tmp_path]


def test_append_entry_rejects_conflicting_identity(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()
    first_message = Message(role="assistant", content="first")
    first = MessageSessionEntry(
        id="entry-1",
        session_id=session.session_id,
        message=first_message,
    )
    conflicting = MessageSessionEntry(
        id=first.id,
        session_id=session.session_id,
        message=first_message.model_copy(update={"content": "different"}),
        created_at=first.created_at,
    )

    async def write() -> None:
        await session.append_entry(first)
        reopened = JsonlSessionStore(tmp_path).load(session.path)
        with pytest.raises(SessionError, match="conflicts with persisted data"):
            await reopened.append_entry(conflicting)

    anyio.run(write)

    assert session.read_entries() == (first,)


def test_append_entry_rejects_another_session(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()
    entry = MessageSessionEntry(
        id="entry-1",
        session_id="another-session",
        message=Message(role="assistant", content="done"),
    )

    async def write() -> None:
        await session.append_entry(entry)

    with pytest.raises(SessionError, match="belongs to another-session"):
        anyio.run(write)
    assert not session.path.exists()


def test_truncate_rewrite_failure_preserves_original_session(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    first = MessageSessionEntry(
        id="entry-1",
        session_id=session.session_id,
        message=Message(role="user", content="first"),
    )
    second = MessageSessionEntry(
        id="entry-2",
        session_id=session.session_id,
        message=Message(role="assistant", content="second"),
    )

    async def seed() -> None:
        await session.append_entry(first)
        await session.append_entry(second)

    anyio.run(seed)
    original = session.path.read_bytes()
    real_write = os.write
    calls = 0

    def fail_after_short_write(fd: int, data: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, data[:7])
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(os, "write", fail_after_short_write)

    async def truncate() -> None:
        await session.truncate_entries(1)

    with pytest.raises(OSError, match="disk full"):
        anyio.run(truncate)

    assert session.path.read_bytes() == original
    assert session.read_entries() == (
        first,
        second.model_copy(update={"parent_id": first.id}),
    )
    assert tuple(tmp_path.glob(f".{session.path.name}.*.tmp")) == ()


def test_truncate_to_zero_propagates_deletion_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def seed() -> None:
        await session.append_message(Message(role="user", content="first"))

    anyio.run(seed)
    original = session.path.read_bytes()
    real_unlink = Path.unlink

    def reject_session_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == session.path:
            raise OSError(errno.EROFS, "read-only file system")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", reject_session_unlink)

    async def truncate() -> None:
        await session.truncate_entries(0)

    with pytest.raises(OSError, match="read-only file system"):
        anyio.run(truncate)

    assert session.path.read_bytes() == original
    assert len(session.read_entries()) == 1


def test_truncate_to_zero_rejects_replaced_file(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def seed() -> None:
        await session.append_message(Message(role="user", content="first"))

    anyio.run(seed)
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(session.path.read_bytes())
    real_validate = session._validate_session_file  # noqa: SLF001

    def replace_after_validation() -> os.stat_result | None:
        info = real_validate()
        if info is not None:
            os.replace(replacement, session.path)
        return info

    monkeypatch.setattr(session, "_validate_session_file", replace_after_validation)

    async def truncate() -> None:
        await session.truncate_entries(0)

    with pytest.raises(SessionError, match="changed before deletion"):
        anyio.run(truncate)

    assert session.path.exists()


def test_truncate_to_zero_syncs_parent_directory(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def seed() -> None:
        await session.append_message(Message(role="user", content="first"))

    anyio.run(seed)
    synced: list[Path] = []
    real_sync = jsonl_module.sync_directory  # noqa: SLF001

    def track_sync(path: Path) -> None:
        synced.append(path)
        real_sync(path)

    monkeypatch.setattr(jsonl_module, "sync_directory", track_sync)

    async def truncate() -> None:
        await session.truncate_entries(0)

    anyio.run(truncate)

    assert not session.path.exists()
    assert synced == [tmp_path]


def test_truncate_invalidates_append_identity_index(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()
    first = MessageSessionEntry(
        id="entry-1",
        session_id=session.session_id,
        message=Message(role="user", content="first"),
    )
    second = MessageSessionEntry(
        id="entry-2",
        session_id=session.session_id,
        message=Message(role="assistant", content="second"),
    )

    async def write() -> None:
        await session.append_entry(first)
        await session.append_entry(second)
        await session.truncate_entries(1)
        await session.append_entry(second)

    anyio.run(write)

    assert session.read_entries() == (
        first,
        second.model_copy(update={"parent_id": first.id}),
    )


def test_recovery_uses_process_local_and_sidecar_locks(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    committed = MessageSessionEntry(
        id="committed",
        session_id=session.session_id,
        message=Message(role="user", content="safe"),
    )
    session.path.write_bytes(f"{session_entry_to_json(committed)}\n{{".encode())
    real_interprocess_lock = jsonl_module.interprocess_lock  # noqa: SLF001
    real_recover = jsonl_module.recover_incomplete_tail  # noqa: SLF001
    sidecar_lock_held = False

    @contextmanager
    def tracked_interprocess_lock(
        path: Path,
        *,
        prepare_parent: bool = True,
    ) -> Iterator[None]:
        nonlocal sidecar_lock_held
        with real_interprocess_lock(path, prepare_parent=prepare_parent):
            sidecar_lock_held = True
            try:
                yield
            finally:
                sidecar_lock_held = False

    def checked_recover(path: Path) -> bool:
        assert session._file_state.lock.locked()  # noqa: SLF001
        assert sidecar_lock_held
        return real_recover(path)

    monkeypatch.setattr(jsonl_module, "interprocess_lock", tracked_interprocess_lock)
    monkeypatch.setattr(jsonl_module, "recover_incomplete_tail", checked_recover)

    assert session.read_entries() == (committed,)


def test_store_creates_private_directories_and_files(tmp_path: Path) -> None:
    root = tmp_path / "missing" / "sessions"
    session = JsonlSessionStore(root).create()

    async def write() -> None:
        await session.append_message(Message(role="user", content="hello"))

    anyio.run(write)

    if os.name == "posix":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(session.path.stat().st_mode) == 0o600


def test_store_secures_existing_session_directory(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    if os.name == "posix":
        root.chmod(0o777)
    session = JsonlSessionStore(root).create()

    async def write() -> None:
        await session.append_message(Message(role="user", content="hello"))

    anyio.run(write)

    if os.name == "posix":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(session.path.stat().st_mode) == 0o600


def test_store_rejects_symlink_session_directory(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported")
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "sessions"
    root.symlink_to(target, target_is_directory=True)
    session = JsonlSessionStore(root).create()

    async def write() -> None:
        await session.append_message(Message(role="user", content="hello"))

    with pytest.raises(SessionError, match="not a directory"):
        anyio.run(write)


def test_read_preserves_existing_parent_permissions(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX permissions are required")
    root = tmp_path / "shared-sessions"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    entry = MessageSessionEntry(
        id="entry-1",
        session_id="session-id",
        message=Message(role="user", content="hello"),
    )
    path = root / "session.jsonl"
    path.write_text(f"{session_entry_to_json(entry)}\n", encoding="utf-8")

    assert JsonlSessionStore(root).load(path).read_entries() == (entry,)

    assert stat.S_IMODE(root.stat().st_mode) == 0o755


@pytest.mark.parametrize("existing_lock", [False, True])
def test_complete_session_reads_do_not_require_write_access(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    existing_lock: bool,
) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    entry = MessageSessionEntry(
        id="entry-1",
        session_id="session-id",
        message=Message(role="user", content="hello"),
    )
    path = root / "session.jsonl"
    path.write_text(f"{session_entry_to_json(entry)}\n", encoding="utf-8")
    lock_path = path.with_suffix(f"{path.suffix}.lock")
    if existing_lock:
        lock_path.touch()
    real_open = os.open

    def reject_session_write_open(
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if os.fsdecode(target) == os.fsdecode(path) and flags & os.O_RDWR:
            raise PermissionError(errno.EACCES, "session is read-only")
        if os.fsdecode(target) == os.fsdecode(lock_path) and flags & os.O_CREAT:
            raise PermissionError(errno.EACCES, "session directory is read-only")
        return real_open(target, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", reject_session_write_open)
    store = JsonlSessionStore(root)

    assert store.load(path).read_entries() == (entry,)
    assert store.latest().read_entries() == (entry,)
    assert store.summaries()[0].session_id == entry.session_id
    assert lock_path.exists() is existing_lock


def test_recovery_rejects_symlink_session_file_without_no_follow(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported")
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    root = tmp_path / "sessions"
    root.mkdir()
    target = tmp_path / "target.jsonl"
    target.write_bytes(b'{"valid":"json"}')
    original = target.read_bytes()
    (root / "linked.jsonl").symlink_to(target)

    with pytest.raises(SessionError, match="not a regular file"):
        JsonlSessionStore(root).summaries()

    assert target.read_bytes() == original


def test_recovery_rejects_symlink_sidecar_lock_without_no_follow(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported")
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    entry = MessageSessionEntry(
        id="entry-1",
        session_id="session-id",
        message=Message(role="user", content="hello"),
    )
    path = tmp_path / "session.jsonl"
    path.write_text(f"{session_entry_to_json(entry)}\n", encoding="utf-8")
    target = tmp_path / "lock-target"
    target.write_bytes(b"")
    original = target.read_bytes()
    path.with_suffix(".jsonl.lock").symlink_to(target)

    with pytest.raises(SessionError, match="lock is not a regular file"):
        JsonlSessionStore(tmp_path).summaries()

    assert target.read_bytes() == original


def test_recovery_rejects_hard_linked_sidecar_lock(tmp_path: Path) -> None:
    entry = MessageSessionEntry(
        id="entry-1",
        session_id="session-id",
        message=Message(role="user", content="hello"),
    )
    path = tmp_path / "session.jsonl"
    path.write_text(f"{session_entry_to_json(entry)}\n", encoding="utf-8")
    target = tmp_path / "lock-target"
    target.write_bytes(b"")
    lock_path = path.with_suffix(".jsonl.lock")
    try:
        os.link(target, lock_path)
    except OSError as exc:
        pytest.skip(f"hard links are not supported: {exc}")
    original_mode = stat.S_IMODE(target.stat().st_mode)

    with pytest.raises(SessionError, match="multiple hard links"):
        JsonlSessionStore(tmp_path).summaries()

    assert target.read_bytes() == b""
    assert stat.S_IMODE(target.stat().st_mode) == original_mode


def test_recovery_rejects_hard_linked_session_file(tmp_path: Path) -> None:
    entry = MessageSessionEntry(
        id="entry-1",
        session_id="session-id",
        message=Message(role="user", content="hello"),
    )
    committed = f"{session_entry_to_json(entry)}\n".encode()
    target = tmp_path / "session-target"
    target.write_bytes(committed + b'{"kind":')
    path = tmp_path / "session.jsonl"
    try:
        os.link(target, path)
    except OSError as exc:
        pytest.skip(f"hard links are not supported: {exc}")
    original = target.read_bytes()

    with pytest.raises(SessionError, match="multiple hard links"):
        JsonlSessionStore(tmp_path).summaries()

    assert target.read_bytes() == original
    assert path.read_bytes() == original


def test_direct_load_rejects_symlink_session_file(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported")
    target = tmp_path / "target.jsonl"
    target.write_bytes(b'{"valid":"json"}')
    original = target.read_bytes()
    link = tmp_path / "linked.jsonl"
    link.symlink_to(target)

    with pytest.raises(SessionError, match="not a regular file"):
        JsonlSessionStore(tmp_path).load(link)

    assert target.read_bytes() == original


def test_append_entry_rejects_symlink_session_file(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported")
    root = tmp_path / "sessions"
    root.mkdir()
    session = JsonlSessionStore(root).create()
    entry = MessageSessionEntry(
        id="entry-1",
        session_id=session.session_id,
        message=Message(role="assistant", content="done"),
    )
    target = tmp_path / "target.jsonl"
    target.write_text(f"{entry.model_dump_json(exclude_none=True)}\n", encoding="utf-8")
    original = target.read_bytes()
    session.path.symlink_to(target)

    async def write() -> None:
        await session.append_entry(entry)

    with pytest.raises(SessionError, match="not a regular file"):
        anyio.run(write)
    assert target.read_bytes() == original
