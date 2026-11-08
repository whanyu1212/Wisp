"""`CodingSession.cancel_run` stops the active run and reports it as cancelled."""

from __future__ import annotations

from pathlib import Path

import anyio

from tests.agent.loop.support import tool_call_stream
from tests.coding.session_support import EchoTool
from wisp.coding.session import CodingSession
from wisp.events import AgentCompleted, TurnCompleted, WispEvent
from wisp.providers.events import ProviderResponseCompleted, ProviderResponseStarted, ToolCall
from wisp.providers.fake import ScriptedProvider
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.context import ToolContext


def _agent(tmp_path: Path) -> CodingSession:
    call = ToolCall(call_id="call-1", name="echo", arguments={"text": "hi"})
    provider = ScriptedProvider(
        [
            tool_call_stream(call),
            [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="done")],
        ]
    )
    tools = ToolRegistry()
    tools.register(EchoTool())
    return CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=tools,
        tool_context=ToolContext(cwd=tmp_path),
    )


def test_cancel_between_turns(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    accepted: list[bool] = []

    async def run() -> list[WispEvent]:
        events: list[WispEvent] = []
        async for event in agent.run("echo"):
            events.append(event)
            if isinstance(event, TurnCompleted) and not accepted:
                # The tool turn completed and the next has not started.
                accepted.append(agent.cancel_run())
        return events

    events = anyio.run(run)

    assert accepted == [True]
    completed = [event for event in events if isinstance(event, AgentCompleted)]
    assert [event.outcome for event in completed] == ["cancelled"]


def test_idle_session_refuses(tmp_path: Path) -> None:
    agent = _agent(tmp_path)

    async def run() -> list[str]:
        events = [event async for event in agent.run("echo")]
        return [event.outcome for event in events if isinstance(event, AgentCompleted)]

    assert agent.cancel_run() is False
    assert anyio.run(run) == ["completed"]
    assert agent.cancel_run() is False
