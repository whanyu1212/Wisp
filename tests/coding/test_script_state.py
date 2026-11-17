"""Script state commits with tool results and follows durable session branches."""

from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError

from tests.cli.rpc_mode_support import VALID_COMPACTION_SUMMARY
from tests.coding.test_codemode import _done, _script, _Tools
from wisp.coding.session import CodingSession
from wisp.events import (
    AgentCompleted,
    CompactionCompleted,
    ToolNestedCallStarted,
    ToolResultReady,
    WispEvent,
)
from wisp.messages import Message
from wisp.providers.events import ProviderEvent, ProviderResponseCompleted, ProviderResponseStarted
from wisp.providers.fake import ScriptedProvider
from wisp.sessions import MessageSessionEntry
from wisp.sessions.entries import SessionEntry, session_entry_from_json, session_entry_to_json
from wisp.sessions.errors import StaleSessionWriterError
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy


def _agent(tmp_path: Path, streams: list[list[ProviderEvent]]) -> CodingSession:
    return CodingSession(
        provider=ScriptedProvider(streams),
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=_Tools().registry(),
        tool_context=ToolContext(cwd=tmp_path),
        tool_policy=ToolPolicy.allow_all_tools(),
        codemode=True,
        auto_compaction_enabled=False,
    )


async def _run(agent: CodingSession, session: JsonlSession) -> list[WispEvent]:
    return [event async for event in agent.run("go", session=session)]


def _outputs(events: list[WispEvent]) -> list[str]:
    results = [event for event in events if isinstance(event, ToolResultReady)]
    assert results and all(not result.is_error for result in results)
    return [result.output for result in results]


def test_state_resumes_without_fetch(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()
    first = _agent(tmp_path, [_script('await store("rows", [1, 2, 3])'), _done()])
    anyio.run(_run, first, session)
    state_entries = [
        entry
        for entry in session.read_entries()
        if isinstance(entry, MessageSessionEntry) and entry.codemode_state is not None
    ]
    assert len(state_entries) == 1
    assert state_entries[0].message.tool_name == "codemode"
    assert state_entries[0].codemode_state == {"rows": [1, 2, 3]}
    assert session_entry_from_json(session_entry_to_json(state_entries[0])) == state_entries[0]

    resumed = _agent(tmp_path, [_script('print(await load("rows"))'), _done()])
    events = anyio.run(_run, resumed, store.load(session.path))
    assert "[1, 2, 3]" in _outputs(events)[0]
    # Reads and no-op scripts must not append redundant snapshots.
    assert (
        sum(
            isinstance(entry, MessageSessionEntry) and entry.codemode_state is not None
            for entry in session.read_entries()
        )
        == 1
    )


def test_branches_and_sessions_are_isolated(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()

    async def branch() -> None:
        agent = _agent(
            tmp_path,
            [
                _script('await store("color", "blue")'),
                _done(),
                _script('await store("color", "red")'),
                _done(),
                _script('print(await load("color"))'),
                _done(),
            ],
        )
        await _run(agent, session)
        original = session.read_active_leaf_id()
        assert original is not None
        await _run(agent, session)
        assert session.read_context().codemode_state == {"color": "red"}
        await session.select_active_leaf(
            original, expected_active_leaf_id=session.read_active_leaf_id()
        )
        assert "blue" in _outputs(await _run(agent, session))[0]
        cloned = await store.clone(session, expected_active_leaf_id=session.read_active_leaf_id())
        writer = _agent(
            tmp_path,
            [
                _script('await delete("color")'),
                _done(),
                _script('try:\n    await load("color")\nexcept KeyError:\n    print("missing")'),
                _done(),
            ],
        )
        await _run(writer, cloned)
        assert cloned.read_context().codemode_state == {}
        assert session.read_context().codemode_state == {"color": "blue"}
        empty = store.create()
        assert "missing" in _outputs(await _run(writer, empty))[0]
        assert empty.read_context().codemode_state == {}

    anyio.run(branch)


def test_compaction_retains_hidden_state(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()
    summary = [
        ProviderResponseStarted(model="test"),
        ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY),
    ]

    async def compact() -> None:
        agent = _agent(
            tmp_path,
            [
                _script('await store("payload", "hidden-state-value")'),
                _done(),
                _done(),
                summary,
            ],
        )
        await _run(agent, session)
        await _run(agent, session)
        events = [event async for event in agent.compact(session)]
        assert any(
            isinstance(event, CompactionCompleted) and event.outcome == "completed"
            for event in events
        )
        replay = session.read_context()
        assert replay.codemode_state == {"payload": "hidden-state-value"}
        assert all("hidden-state-value" not in row.message.content for row in replay.rows)
        resumed = _agent(tmp_path, [_script('print(await load("payload"))'), _done()])
        assert "hidden-state-value" in _outputs(await _run(resumed, store.load(session.path)))[0]

    anyio.run(compact)


def test_failed_script_does_not_persist(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path / "sessions").create()
    agent = _agent(
        tmp_path,
        [
            _script('await store("value", "kept")'),
            _done(),
            _script('await store("value", "lost")\nraise ValueError("abort")'),
            _done(),
            _script('print(await load("value"))'),
            _done(),
        ],
    )

    async def run() -> None:
        await _run(agent, session)
        failed = await _run(agent, session)
        assert any(isinstance(event, ToolResultReady) and event.is_error for event in failed)
        assert session.read_context().codemode_state == {"value": "kept"}
        assert "kept" in _outputs(await _run(agent, session))[0]

    anyio.run(run)


@pytest.mark.parametrize(
    "message",
    [
        Message(role="user", content="bad"),
        Message(role="tool", tool_name="read", tool_call_id="c", content="bad"),
        Message(role="tool", tool_name="codemode", tool_call_id="c", content="bad", is_error=True),
    ],
)
def test_state_requires_successful_result(message: Message) -> None:
    with pytest.raises(ValidationError, match="successful codemode"):
        MessageSessionEntry(session_id="s", message=message, codemode_state={"key": "value"})


def test_cancellation_discards_staged_state(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path / "sessions").create()
    agent = _agent(
        tmp_path,
        [
            _script('await store("value", "kept")'),
            _done(),
            _script('await store("value", "cancelled")\nawait lookup(table="cancel")'),
            _done(),
        ],
    )
    cancelled = []

    async def cancel(event: WispEvent) -> None:
        if isinstance(event, ToolNestedCallStarted):
            cancelled.append(agent.cancel_run())

    async def run() -> None:
        await _run(agent, session)
        events = [
            event async for event in agent.run("cancel", session=session, live_event_sink=cancel)
        ]
        assert cancelled == [True]
        assert any(
            isinstance(event, AgentCompleted) and event.outcome == "cancelled" for event in events
        )
        assert session.read_context().codemode_state == {"value": "kept"}
        assert all(
            entry.codemode_state != {"value": "cancelled"}
            for entry in session.read_entries()
            if isinstance(entry, MessageSessionEntry)
        )

    anyio.run(run)


def test_state_append_retry_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = JsonlSessionStore(tmp_path / "sessions").create()
    original = session.append_entry_if_current
    failed = False

    async def lose_receipt(
        entry: SessionEntry, *, expected_active_leaf_id: str | None
    ) -> SessionEntry:
        nonlocal failed
        persisted = await original(entry, expected_active_leaf_id=expected_active_leaf_id)
        if (
            isinstance(entry, MessageSessionEntry)
            and entry.codemode_state is not None
            and not failed
        ):
            failed = True
            raise OSError("lost append receipt")
        return persisted

    monkeypatch.setattr(session, "append_entry_if_current", lose_receipt)
    agent = _agent(tmp_path, [_script('await store("value", "durable")'), _done()])
    with pytest.raises(OSError, match="lost append receipt"):
        anyio.run(_run, agent, session)
    snapshots = [
        entry
        for entry in session.read_entries()
        if isinstance(entry, MessageSessionEntry) and entry.codemode_state is not None
    ]
    assert len(snapshots) == 1
    assert session.read_context().codemode_state == {"value": "durable"}
    resumed = _agent(tmp_path, [_script('print(await load("value"))'), _done()])
    assert "durable" in _outputs(anyio.run(_run, resumed, session))[0]


def test_stale_writer_discards_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = JsonlSessionStore(tmp_path / "sessions").create()
    original = session.append_entry_if_current
    moved = False

    async def move_branch(
        entry: SessionEntry, *, expected_active_leaf_id: str | None
    ) -> SessionEntry:
        nonlocal moved
        if (
            isinstance(entry, MessageSessionEntry)
            and entry.codemode_state is not None
            and not moved
        ):
            moved = True
            await session.append_message(Message(role="assistant", content="another writer"))
        return await original(entry, expected_active_leaf_id=expected_active_leaf_id)

    monkeypatch.setattr(session, "append_entry_if_current", move_branch)
    agent = _agent(tmp_path, [_script('await store("value", "stale")'), _done()])
    with pytest.raises(StaleSessionWriterError):
        anyio.run(_run, agent, session)
    assert session.read_context().codemode_state == {}
    assert all(
        entry.codemode_state is None
        for entry in session.read_entries()
        if isinstance(entry, MessageSessionEntry)
    )
