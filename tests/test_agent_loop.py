from __future__ import annotations

import json
from pathlib import Path

import anyio

from wisp.agent.loop import Agent
from wisp.events import AssistantMessage, SessionSaved, TokenDelta
from wisp.providers.fake import FakeProvider
from wisp.sessions.jsonl import JsonlSessionStore


def test_agent_streams_fake_response_and_saves_session(tmp_path: Path) -> None:
    async def run_agent() -> list[object]:
        agent = Agent(provider=FakeProvider(), sessions=JsonlSessionStore(tmp_path))
        return [event async for event in agent.run("hello")]

    events = anyio.run(run_agent)
    deltas = [event.delta for event in events if isinstance(event, TokenDelta)]

    assert "".join(deltas) == "fake response to: hello"
    assert any(
        isinstance(event, AssistantMessage) and event.content == "fake response to: hello"
        for event in events
    )

    saved = next(event for event in events if isinstance(event, SessionSaved))
    assert saved.path.exists()

    records = [json.loads(line) for line in saved.path.read_text(encoding="utf-8").splitlines()]
    assert [record["message"]["role"] for record in records] == ["user", "assistant"]
    assert [record["message"]["content"] for record in records] == [
        "hello",
        "fake response to: hello",
    ]
