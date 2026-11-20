from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

import anyio
import pytest

from tests.coding.compaction_support import build_model_registry
from tests.coding.session_support import ToolLoopProvider
from wisp.coding.saved_session import ReadSavedSessionTool
from wisp.coding.session import CodingSession
from wisp.events import (
    AgentStarted,
    QueueMessageInjected,
    SessionDirectoryChanged,
    ToolExecutionEnded,
)
from wisp.messages import Message
from wisp.providers.events import ToolCall
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolError

_TOOL = "read_saved_session"


def _call(number: int, *, response: int | None = None, **arguments: object) -> ToolCall:
    return ToolCall(
        call_id=f"call-{number}",
        response_id=f"response-{response if response is not None else number}",
        name=_TOOL,
        arguments=arguments,
    )


def test_only_shared_source_is_read(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = JsonlSessionStore(tmp_path / "sessions")
        selected, foreign, current = (store.create() for _ in range(3))
        root = await selected.append_message(Message(role="user", content="selected prompt"))
        discarded = await selected.append_message(
            Message(role="assistant", content="discarded branch")
        )
        await selected.select_active_leaf(root.id, expected_active_leaf_id=discarded.id)
        image = await selected.store_image(
            (Path(__file__).parents[1] / "fixtures" / "images" / "pixel.png").read_bytes()
        )
        user = await selected.append_message(
            Message(role="user", content="selected question", images=(image,))
        )
        await selected.append_message(Message(role="system", content="old system text"))
        await selected.append_message(
            Message(role="tool", content="old tool output", tool_call_id="old-tool")
        )
        await selected.append_event(
            SessionDirectoryChanged(session_id=selected.session_id, cwd=Path("/private-folder"))
        )
        await selected.append_message(Message(role="assistant", content="selected reply"))
        await foreign.append_message(Message(role="user", content="unshared text"))
        await current.append_message(Message(role="user", content="current conversation"))
        provider = ToolLoopProvider(
            [
                [
                    _call(1, session_id=foreign.session_id),
                    _call(2, response=1),
                    _call(3, response=1, before="guessed"),
                    _call(4, response=1, before=1),
                ],
                ["done"],
                [_call(5)],
                ["done again"],
            ]
        )
        agent = CodingSession(
            provider=provider,
            sessions=store,
            tool_context=ToolContext(cwd=tmp_path),
            tool_policy=ToolPolicy.allow_no_tools(),
        )
        first = [
            event
            async for event in agent.run(
                "shared", session=current, saved_session_id=selected.session_id
            )
        ]
        second = [event async for event in agent.run("next", session=current)]

        starts = [event for event in (*first, *second) if isinstance(event, AgentStarted)]
        assert [event.session_id for event in starts] == [current.session_id, current.session_id]
        assert [event.setup.tool_names for event in starts if event.setup is not None] == [
            (_TOOL,),
            (),
        ]
        denied, read, unknown_cursor, mistyped_cursor = provider.calls[1][0]
        assert denied.is_error and "only a before cursor" in denied.output
        for invalid_cursor in (unknown_cursor, mistyped_cursor):
            assert invalid_cursor.is_error and "next_before cursor" in invalid_cursor.output
        assert not read.is_error
        summary, encoded = read.output.split("\n", maxsplit=1)
        assert summary == "Read 3 saved session messages"
        payload = json.loads(encoded)
        assert payload["session_id"] == selected.session_id
        assert [message["text"] for message in payload["messages"]] == [
            "selected prompt",
            "selected question",
            "selected reply",
        ]
        assert payload["messages"][1]["entry_id"] == user.id
        assert set(payload["messages"][1]) == {"entry_id", "role", "text", "truncated"}
        (ungranted,) = provider.calls[3][0]
        assert ungranted.is_error
        assert "selected reply" not in ungranted.output
        assert [
            message.content for message in current.read_messages() if message.role == "user"
        ] == ["current conversation", "shared", "next"]

    anyio.run(scenario)


@pytest.mark.parametrize("kind", ["steer", "follow_up", "preflight"])
def test_queue_delivery_revokes_permission(
    tmp_path: Path, kind: Literal["steer", "follow_up", "preflight"]
) -> None:
    async def scenario() -> None:
        store = JsonlSessionStore(tmp_path / "sessions")
        selected = store.create()
        await selected.append_message(Message(role="user", content="shared historical text"))
        turns: list[list[object]] = [[_call(2)], ["done"]]
        models = None
        if kind == "preflight":
            models = build_model_registry(context_window=100_000, auto_compact_token_limit=50_000)
        else:
            if kind == "follow_up":
                turns.insert(0, ["initial answer"])
            turns.insert(0, [_call(1)])
        provider = ToolLoopProvider(turns)
        if kind == "preflight":
            provider.name, provider.default_model = "scripted", "model"
        agent = CodingSession(
            provider=provider, sessions=store, tool_context=ToolContext(cwd=tmp_path), models=models
        )
        timeline: list[QueueMessageInjected | ToolExecutionEnded] = []
        async for event in agent.run("first", saved_session_id=selected.session_id):
            if isinstance(event, AgentStarted):
                await (agent.follow_up if kind == "follow_up" else agent.steer)("later input")
            elif isinstance(event, QueueMessageInjected | ToolExecutionEnded):
                timeline.append(event)

        if kind != "preflight":
            first = timeline.pop(0)
            assert isinstance(first, ToolExecutionEnded)
            assert not first.is_error and "shared historical text" in first.output
        injected, denied = timeline
        assert isinstance(injected, QueueMessageInjected) and injected.content == "later input"
        assert isinstance(denied, ToolExecutionEnded) and denied.call_id == "call-2"
        assert denied.is_error and "later user message" in denied.output
        assert "shared historical text" not in denied.output

    anyio.run(scenario)


@pytest.mark.parametrize(
    "failure", ["outside", "symlink", "hardlink", "duplicate", "current", "corrupt"]
)
def test_unsafe_source_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    async def scenario() -> None:
        store = JsonlSessionStore(tmp_path / "sessions")
        foreign_store = JsonlSessionStore(tmp_path / "foreign")
        current = store.create()
        selected = (
            current
            if failure == "current"
            else store.create()
            if failure in {"duplicate", "corrupt"}
            else foreign_store.create()
        )
        await selected.append_message(Message(role="user", content="private"))
        store.root.mkdir(exist_ok=True)
        alias = store.root / "alias.jsonl"
        if failure == "outside":
            monkeypatch.chdir(selected.path.parent)
            selected.path.rename(selected.path.with_name(selected.session_id))
        elif failure == "symlink":
            alias.symlink_to(selected.path)
        elif failure == "hardlink":
            os.link(selected.path, alias)
        elif failure == "duplicate":
            alias.write_bytes(selected.path.read_bytes())
        elif failure == "corrupt":
            with selected.path.open("ab") as stream:
                stream.write(b"corrupt complete record\n")
        provider = ToolLoopProvider([["must not be called"]])
        agent = CodingSession(
            provider=provider, sessions=store, tool_context=ToolContext(cwd=tmp_path)
        )
        with pytest.raises(ValueError, match="saved session|current session") as failure_event:
            _ = [
                event
                async for event in agent.run(
                    "prompt", session=current, saved_session_id=selected.session_id
                )
            ]
        assert str(store.root) not in str(failure_event.value)
        assert provider.calls == []
        if failure != "current":
            assert not current.path.exists()

    anyio.run(scenario)


@pytest.mark.parametrize("cancel_after", [1, 4])
def test_lookup_checks_cancellation(tmp_path: Path, cancel_after: int) -> None:
    store = JsonlSessionStore(tmp_path)
    selected = store.create()

    async def write() -> None:
        for _ in range(4):
            await selected.append_message(Message(role="user", content="complete record"))

    anyio.run(write)
    original = selected.path.read_bytes()
    checks = 0

    def check_cancelled() -> None:
        nonlocal checks
        checks += 1
        if checks == cancel_after:
            raise RuntimeError("cancelled scan")

    with pytest.raises(RuntimeError, match="cancelled scan"):
        store.saved_session_for_prompt(
            selected.session_id, current=store.create(), check_cancelled=check_cancelled
        )
    assert selected.path.read_bytes() == original


@pytest.mark.parametrize("large", [False, True])
def test_pages_are_bounded(tmp_path: Path, large: bool) -> None:
    async def scenario() -> None:
        store = JsonlSessionStore(tmp_path)
        selected = store.create()
        for index in range(33):
            content = "🙂" * (5000 if large else 1)
            await selected.append_message(Message(role="user", content=f"{index} {content}"))
        contents = selected.path.read_bytes() + b"part of an incomplete record"
        selected.path.write_bytes(contents)
        lock = selected.path.with_suffix(".jsonl.lock")
        lock.unlink()
        tool = ReadSavedSessionTool(
            store.saved_session_for_prompt(selected.session_id, current=store.create())
        )
        context = ToolContext(cwd=tmp_path)
        total_bytes = 0
        seen: list[int] = []
        arguments: dict[str, object] = {}
        for _ in range(8):
            try:
                result = await tool.run(arguments, context)
            except ToolError as exc:
                assert "limit" in str(exc)
                break
            total_bytes += len(result.text.encode("utf-8"))
            page = json.loads(result.text.split("\n", maxsplit=1)[1])
            assert len(page["messages"]) == 4
            assert all(message["truncated"] is large for message in page["messages"])
            seen.extend(int(message["text"].partition(" ")[0]) for message in page["messages"])
            arguments = {"before": page["next_before"]} if page["next_before"] else {}
        assert seen[:8] == [29, 30, 31, 32, 25, 26, 27, 28]
        if not large:
            assert len(seen) == 32 and arguments == {}
        assert 0 < total_bytes <= 64 * 1024
        with pytest.raises(ToolError, match="limit"):
            await tool.run({}, context)
        assert selected.path.read_bytes() == contents
        assert not lock.exists()

    anyio.run(scenario)


@pytest.mark.parametrize("mutation", ["replace", "append", "delete"])
def test_changed_source_revokes_snapshot(tmp_path: Path, mutation: str) -> None:
    async def scenario() -> None:
        store = JsonlSessionStore(tmp_path)
        selected = store.create()
        await selected.append_message(Message(role="user", content="original text"))
        tool = ReadSavedSessionTool(
            store.saved_session_for_prompt(selected.session_id, current=store.create())
        )
        context = ToolContext(cwd=tmp_path)
        assert "original text" in (await tool.run({}, context)).text
        if mutation == "replace":
            other = store.create()
            await other.append_message(Message(role="user", content="other text"))
            other.path.replace(selected.path)
        elif mutation == "append":
            await selected.append_message(Message(role="assistant", content="new content"))
        else:
            selected.path.unlink()
        with pytest.raises(ToolError, match="unavailable"):
            await tool.run({}, context)

    anyio.run(scenario)
