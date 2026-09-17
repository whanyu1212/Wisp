from __future__ import annotations

import json
import os
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import anyio
import pytest
from pytest import MonkeyPatch

from wisp.agent.messages import CompactionRecord, Message
from wisp.events import ErrorEvent
from wisp.sessions import jsonl as jsonl_module
from wisp.sessions.branching import SessionBranchProjection
from wisp.sessions.entries import (
    CompactionSessionEntry,
    MessageSessionEntry,
    SessionEntry,
    SessionInfoSessionEntry,
    SessionTreeEntry,
    is_session_tree_entry,
    session_entry_to_json,
)
from wisp.sessions.errors import (
    InvalidSessionBranchPointError,
    SessionError,
    StaleSessionTreeError,
)
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore
from wisp.sessions.replay import SessionReplayError


def test_clone_copies_only_active_path_and_preserves_entry_identity(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    source = store.create()

    async def seed_and_clone() -> tuple[JsonlSession, bytes]:
        await source.append_message(Message(role="user", content="root"))
        root_answer = await source.append_message(Message(role="assistant", content="answer"))
        await source.append_message(Message(role="user", content="abandoned"))
        abandoned_answer = await source.append_message(
            Message(role="assistant", content="abandoned answer")
        )
        await source.select_active_leaf(
            root_answer.id,
            expected_active_leaf_id=abandoned_answer.id,
        )
        branch = await source.append_message(Message(role="user", content="branch"))
        await source.append_event(ErrorEvent(message="retained audit event"))
        branch_answer = await source.append_message(
            Message(role="assistant", content="branch answer")
        )
        source_before_clone = source.path.read_bytes()
        cloned = await store.clone(
            source,
            expected_active_leaf_id=branch_answer.id,
        )
        assert isinstance(branch, MessageSessionEntry)
        assert branch.parent_id == root_answer.id
        return cloned, source_before_clone

    cloned, source_before_clone = anyio.run(seed_and_clone)

    source_path = cast(tuple[SessionTreeEntry, ...], source.read_active_path())
    loaded_entries = cloned.read_entries()
    assert all(is_session_tree_entry(entry) for entry in loaded_entries)
    copied = cast(tuple[SessionTreeEntry, ...], loaded_entries)
    assert [entry.id for entry in copied] == [entry.id for entry in source_path]
    assert [entry.parent_id for entry in copied] == [entry.parent_id for entry in source_path]
    assert all(entry.session_id == cloned.session_id for entry in copied)
    assert cloned.session_id != source.session_id
    assert [message.content for message in cloned.read_context_messages()] == [
        "root",
        "answer",
        "branch",
        "branch answer",
    ]
    assert cloned.read_active_leaf_id() == copied[-1].id
    assert source.path.read_bytes() == source_before_clone

    reloaded = store.load(cloned.path)
    assert reloaded.read_entries() == copied
    assert reloaded.read_context_messages() == cloned.read_context_messages()


def test_clone_inherits_effective_name_with_new_metadata_entry(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    source = store.create()

    async def seed_and_clone() -> JsonlSession:
        leaf = await source.append_message(Message(role="user", content="root"))
        await source.set_name("Named Source")
        return await store.clone(source, expected_active_leaf_id=leaf.id)

    cloned = anyio.run(seed_and_clone)

    assert source.read_name() == "Named Source"
    assert cloned.read_name() == "Named Source"
    source_info = next(
        entry for entry in source.read_entries() if isinstance(entry, SessionInfoSessionEntry)
    )
    cloned_info = next(
        entry for entry in cloned.read_entries() if isinstance(entry, SessionInfoSessionEntry)
    )
    assert cloned_info.id != source_info.id
    assert cloned_info.session_id == cloned.session_id


def test_fork_starts_unnamed_and_preserves_source_name(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    source = store.create()

    async def seed_and_fork() -> JsonlSession:
        await source.set_name("Named Source")
        selected = await source.append_message(Message(role="user", content="edit me"))
        result = await store.fork_from_user_message(
            source,
            selected.id,
            expected_active_leaf_id=selected.id,
        )
        assert result.source_session_name == "Named Source"
        return result.session

    forked = anyio.run(seed_and_fork)

    assert source.read_name() == "Named Source"
    assert forked.read_name() is None
    assert not forked.path.exists()


def test_clone_to_leaf_copies_an_inactive_user_or_assistant_path(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    source = store.create()

    async def copy_paths() -> tuple[JsonlSession, JsonlSession]:
        user = await source.append_message(Message(role="user", content="one"))
        assistant = await source.append_message(Message(role="assistant", content="two"))
        active = await source.append_message(Message(role="user", content="three"))
        return (
            await store.clone_to_leaf(
                source,
                user.id,
                expected_active_leaf_id=active.id,
            ),
            await store.clone_to_leaf(
                source,
                assistant.id,
                expected_active_leaf_id=active.id,
            ),
        )

    user_clone, assistant_clone = anyio.run(copy_paths)
    assert [message.content for message in user_clone.read_messages()] == ["one"]
    assert [message.content for message in assistant_clone.read_messages()] == ["one", "two"]


def test_fork_excludes_selected_user_message_and_returns_its_text(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    source = store.create()

    async def fork() -> tuple[JsonlSession, str, str]:
        await source.append_message(Message(role="user", content="first"))
        await source.append_message(Message(role="assistant", content="answer"))
        selected = await source.append_message(Message(role="user", content="edit me"))
        active = await source.append_message(Message(role="assistant", content="old answer"))
        result = await store.fork_from_user_message(
            source,
            selected.id,
            expected_active_leaf_id=active.id,
        )
        assert result.source_session_id == source.session_id
        assert result.source_active_leaf_id == active.id
        assert result.selected_entry_id == selected.id
        return result.session, result.selected_prompt, result.fork_leaf_id or ""

    forked, prompt, source_leaf_id = anyio.run(fork)
    assert prompt == "edit me"
    assert [message.content for message in forked.read_messages()] == ["first", "answer"]
    assert forked.read_active_leaf_id() == source_leaf_id


def test_forking_first_user_message_defers_empty_file_until_resubmission(
    tmp_path: Path,
) -> None:
    store = JsonlSessionStore(tmp_path)
    source = store.create()

    async def fork_and_resubmit() -> JsonlSession:
        first = await source.append_message(Message(role="user", content="original"))
        active = await source.append_message(Message(role="assistant", content="answer"))
        result = await store.fork_from_user_message(
            source,
            first.id,
            expected_active_leaf_id=active.id,
        )
        assert result.selected_prompt == "original"
        assert result.source_active_leaf_id == active.id
        assert result.fork_leaf_id is None
        assert not result.session.path.exists()
        await result.session.append_message(Message(role="user", content="edited"))
        return result.session

    forked = anyio.run(fork_and_resubmit)
    assert [message.content for message in store.load(forked.path).read_messages()] == ["edited"]


@pytest.mark.parametrize("target", ["assistant", "missing"])
def test_fork_rejects_non_user_branch_points(tmp_path: Path, target: str) -> None:
    store = JsonlSessionStore(tmp_path)
    source = store.create()

    async def fork() -> None:
        await source.append_message(Message(role="user", content="one"))
        assistant = await source.append_message(Message(role="assistant", content="two"))
        entry_id = assistant.id if target == "assistant" else "missing"
        with pytest.raises(InvalidSessionBranchPointError, match="persisted user message"):
            await store.fork_from_user_message(
                source,
                entry_id,
                expected_active_leaf_id=assistant.id,
            )

    anyio.run(fork)
    assert tuple(tmp_path.glob("*.jsonl")) == (source.path,)


def test_clone_rejects_stale_leaf_without_creating_destination(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    source = store.create()

    async def clone() -> None:
        stale = await source.append_message(Message(role="user", content="one"))
        await source.append_message(Message(role="assistant", content="two"))
        with pytest.raises(StaleSessionTreeError, match="expected active leaf"):
            await store.clone(source, expected_active_leaf_id=stale.id)

    anyio.run(clone)
    assert tuple(tmp_path.glob("*.jsonl")) == (source.path,)


def test_clone_upgrades_legacy_source_only_in_new_file(tmp_path: Path) -> None:
    source_path = tmp_path / "legacy.jsonl"
    created = datetime(2025, 1, 1, tzinfo=UTC).isoformat()
    records = [
        {
            "id": "legacy-user",
            "session_id": "legacy-session",
            "created_at": created,
            "message": Message(role="user", content="hello").model_dump(mode="json"),
        },
        {
            "id": "legacy-answer",
            "session_id": "legacy-session",
            "created_at": created,
            "message": Message(role="assistant", content="hi").model_dump(mode="json"),
        },
    ]
    source_path.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )
    original = source_path.read_bytes()
    store = JsonlSessionStore(tmp_path)
    source = store.load(source_path)

    async def clone() -> JsonlSession:
        return await store.clone(source, expected_active_leaf_id="legacy-answer")

    cloned = anyio.run(clone)

    assert source_path.read_bytes() == original
    assert [entry.schema_version for entry in cloned.read_entries()] == [6, 6]
    assert [entry.id for entry in cloned.read_entries()] == ["legacy-user", "legacy-answer"]
    assert all(entry.session_id == cloned.session_id for entry in cloned.read_entries())


def test_clone_preserves_compaction_replay_and_metadata(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    source = store.create()

    async def clone() -> JsonlSession:
        first = await source.append_message(Message(role="user", content="one"))
        first_answer = await source.append_message(Message(role="assistant", content="answer one"))
        second = await source.append_message(Message(role="user", content="two"))
        second_answer = await source.append_message(Message(role="assistant", content="answer two"))
        context_ids = source.read_context().context_entry_ids
        compacted = await source.append_compaction_entry(
            CompactionSessionEntry(
                id="compact",
                session_id=source.session_id,
                compaction=CompactionRecord(
                    summary="First turn summary.",
                    replaced_entry_ids=(first.id, first_answer.id),
                    provider="test",
                    model="model",
                ),
            ),
            expected_context_entry_ids=context_ids,
        )
        assert context_ids == (first.id, first_answer.id, second.id, second_answer.id)
        return await store.clone(
            source,
            expected_active_leaf_id=compacted.id,
        )

    cloned = anyio.run(clone)
    assert cloned.read_context() == source.read_context()
    copied_compaction = cloned.read_entries()[-1]
    assert isinstance(copied_compaction, CompactionSessionEntry)
    assert copied_compaction.compaction.provider == "test"
    assert copied_compaction.compaction.model == "model"


def test_clone_rejects_invalid_selected_compaction_before_creating_target(
    tmp_path: Path,
) -> None:
    source = JsonlSessionStore(tmp_path).create()
    user = MessageSessionEntry(
        id="user",
        session_id=source.session_id,
        message=Message(role="user", content="one"),
    )
    invalid = CompactionSessionEntry(
        id="invalid",
        session_id=source.session_id,
        parent_id=user.id,
        compaction=CompactionRecord(
            summary="bad",
            replaced_entry_ids=("missing",),
            provider="test",
        ),
    )
    source.path.write_text(
        f"{session_entry_to_json(user)}\n{session_entry_to_json(invalid)}\n",
        encoding="utf-8",
    )
    store = JsonlSessionStore(tmp_path)
    loaded = store.load(source.path)

    async def clone() -> None:
        with pytest.raises(SessionReplayError, match="unknown"):
            await store.clone(loaded, expected_active_leaf_id=invalid.id)

    anyio.run(clone)
    assert tuple(tmp_path.glob("*.jsonl")) == (source.path,)


def test_failed_destination_validation_removes_new_file(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    store = JsonlSessionStore(tmp_path)
    source = store.create()

    async def seed() -> str:
        entry = await source.append_message(Message(role="user", content="one"))
        return entry.id

    leaf_id = anyio.run(seed)
    target = JsonlSession(session_id="target-session", path=tmp_path / "target.jsonl")
    monkeypatch.setattr(store, "create", lambda: target)
    read_entries = jsonl_module._read_entries_unlocked  # noqa: SLF001

    def fail_target_read(path: Path, *, limit: int | None = None) -> list[SessionEntry]:
        if path != source.path:
            raise SessionError("simulated validation failure")
        return read_entries(path, limit=limit)

    monkeypatch.setattr(jsonl_module, "_read_entries_unlocked", fail_target_read)

    async def clone() -> None:
        with pytest.raises(SessionError, match="simulated validation failure"):
            await store.clone(source, expected_active_leaf_id=leaf_id)

    anyio.run(clone)
    assert not target.path.exists()
    assert not tuple(tmp_path.glob(".*.tmp"))
    assert source.path.is_file()


def test_clone_publishes_only_a_complete_validated_file(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    store = JsonlSessionStore(tmp_path)
    source = store.create()

    async def seed() -> str:
        first = await source.append_message(Message(role="user", content="one"))
        second = await source.append_message(Message(role="assistant", content="two"))
        assert first.id != second.id
        return second.id

    leaf_id = anyio.run(seed)
    target = JsonlSession(session_id="target-session", path=tmp_path / "target.jsonl")
    monkeypatch.setattr(store, "create", lambda: target)
    link = os.link
    observed_entries: tuple[SessionEntry, ...] = ()

    def inspect_before_publish(source_path: Path, destination_path: Path) -> None:
        nonlocal observed_entries
        assert destination_path == target.path
        assert not destination_path.exists()
        observed_entries = tuple(jsonl_module._read_entries(source_path))  # noqa: SLF001
        link(source_path, destination_path)

    monkeypatch.setattr(os, "link", inspect_before_publish)

    async def clone() -> JsonlSession:
        return await store.clone(source, expected_active_leaf_id=leaf_id)

    cloned = anyio.run(clone)
    assert cloned.path == target.path
    assert observed_entries == cloned.read_entries()
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_clone_blocks_destination_append_until_cache_initialization(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    store = JsonlSessionStore(tmp_path)
    source = store.create()

    async def seed() -> str:
        entry = await source.append_message(Message(role="user", content="source"))
        return entry.id

    leaf_id = anyio.run(seed)
    target = JsonlSession(session_id="target-session", path=tmp_path / "target.jsonl")
    monkeypatch.setattr(store, "create", lambda: target)
    link = os.link
    published = threading.Event()
    append_started = threading.Event()
    append_finished = threading.Event()

    def pause_after_publish(source_path: Path, destination_path: Path) -> None:
        link(source_path, destination_path)
        published.set()
        assert append_started.wait(timeout=5)
        assert not append_finished.wait(timeout=0.1)

    monkeypatch.setattr(os, "link", pause_after_publish)
    cloned: JsonlSession | None = None

    async def run_clone() -> None:
        nonlocal cloned
        cloned = await store.clone(source, expected_active_leaf_id=leaf_id)

    async def append_during_publication() -> None:
        assert await anyio.to_thread.run_sync(published.wait, 5)
        append_started.set()
        reopened = store.load(target.path)
        await reopened.append_message(Message(role="assistant", content="concurrent"))
        append_finished.set()

    async def race() -> None:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run_clone)
            task_group.start_soon(append_during_publication)

    anyio.run(race)
    assert cloned is not None
    assert append_finished.is_set()

    async def append_from_creator() -> None:
        assert cloned is not None
        await cloned.append_message(Message(role="user", content="creator"))

    anyio.run(append_from_creator)
    assert [message.content for message in cloned.read_context_messages()] == [
        "source",
        "concurrent",
        "creator",
    ]


def test_clone_never_overwrites_an_existing_destination(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    store = JsonlSessionStore(tmp_path)
    source = store.create()

    async def seed() -> str:
        entry = await source.append_message(Message(role="user", content="one"))
        return entry.id

    leaf_id = anyio.run(seed)
    target_path = tmp_path / "target.jsonl"
    target_path.write_text("do not replace\n", encoding="utf-8")
    target = JsonlSession(session_id="target-session", path=target_path)
    monkeypatch.setattr(store, "create", lambda: target)

    async def clone() -> None:
        with pytest.raises(FileExistsError):
            await store.clone(source, expected_active_leaf_id=leaf_id)

    anyio.run(clone)
    assert target_path.read_text(encoding="utf-8") == "do not replace\n"
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_clone_snapshot_remains_coherent_when_source_appends_after_snapshot(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    store = JsonlSessionStore(tmp_path)
    source = store.create()

    async def seed() -> str:
        entry = await source.append_message(Message(role="user", content="before"))
        return entry.id

    leaf_id = anyio.run(seed)
    snapshot_taken = threading.Event()
    continue_clone = threading.Event()
    persist_projection = store._persist_projection  # noqa: SLF001

    def pause_after_snapshot(
        projection: SessionBranchProjection,
        *,
        name: str | None,
    ) -> JsonlSession:
        snapshot_taken.set()
        assert continue_clone.wait(timeout=5)
        return persist_projection(projection, name=name)

    monkeypatch.setattr(store, "_persist_projection", pause_after_snapshot)
    cloned: JsonlSession | None = None

    async def run_clone() -> None:
        nonlocal cloned
        cloned = await store.clone(source, expected_active_leaf_id=leaf_id)

    async def race_append() -> None:
        assert await anyio.to_thread.run_sync(snapshot_taken.wait, 5)
        await source.append_message(Message(role="assistant", content="after"))
        continue_clone.set()

    async def race() -> None:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run_clone)
            task_group.start_soon(race_append)

    anyio.run(race)
    assert cloned is not None
    assert [message.content for message in cloned.read_messages()] == ["before"]
    assert [message.content for message in source.read_messages()] == ["before", "after"]


def test_clone_target_uses_private_file_permissions(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    source = store.create()

    async def clone() -> JsonlSession:
        leaf = await source.append_message(Message(role="user", content="one"))
        return await store.clone(source, expected_active_leaf_id=leaf.id)

    cloned = anyio.run(clone)
    if os.name == "posix":
        assert stat.S_IMODE(cloned.path.stat().st_mode) == 0o600
