"""Codex transport failures must retain diagnostics and completed work across resume."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import httpx
import pytest

from tests.coding.session_support import EchoTool
from tests.providers.openai_codex_support import store_with_oauth
from wisp.coding.session import CodingSession
from wisp.events import AgentCompleted, ErrorEvent
from wisp.providers.auth import StoredProviderAuthResolver
from wisp.providers.openai_codex import OpenAICodexProvider
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.entries import EventSessionEntry
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.base import ToolArguments
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolResult


def _sse(*events: dict[str, object]) -> bytes:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()


@pytest.mark.parametrize("partial", ["", "partial answer"])
def test_timeout_persists_diagnostics_and_resumes_without_reexecuting_tools(
    tmp_path: Path, partial: str
) -> None:
    requests: list[dict[str, object]] = []
    executions: list[ToolArguments] = []
    call = {
        "type": "function_call",
        "id": "item-1",
        "call_id": "call-1",
        "name": "echo",
        "arguments": '{"text":"once"}',
    }

    class RecordingTool(EchoTool):
        async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
            executions.append(arguments)
            return await super().run(arguments, context)

    class FailingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield _sse(
                {"type": "response.created", "response": {"id": "failed-response"}},
                {"type": "response.output_text.delta", "delta": partial},
                # Even a fully formed tool call must not execute without response.completed.
                {"type": "response.output_item.done", "item": dict(call, call_id="uncommitted")},
            )
            raise httpx.ReadTimeout("")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            content = _sse(
                {"type": "response.output_item.done", "item": call},
                {
                    "type": "response.completed",
                    "response": {"id": "tool-response", "output": [call]},
                },
            )
        elif len(requests) == 2:
            return httpx.Response(
                200, stream=FailingStream(), headers={"content-type": "text/event-stream"}
            )
        else:
            assert len(requests) == 3
            content = _sse(
                {"type": "response.output_text.delta", "delta": "resumed"},
                {"type": "response.completed", "response": {"id": "resumed-response"}},
            )
        return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})

    async def scenario() -> None:
        sessions = JsonlSessionStore(tmp_path / "sessions")
        session = sessions.create()
        tools = ToolRegistry()
        tools.register(RecordingTool())
        auth = StoredProviderAuthResolver(store_with_oauth(tmp_path))
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=httpx.Timeout(10, read=300)
        ) as client:
            provider = OpenAICodexProvider(auth_resolver=auth, client=client)
            agent = CodingSession(
                provider=provider, sessions=sessions, tool_registry=tools, trusted=True
            )
            events = [event async for event in agent.run("echo once", session=session)]
            assert isinstance(events[-1], AgentCompleted)
            assert events[-1].outcome == "failed"
            error = next(event for event in events if isinstance(event, ErrorEvent))
            assert "ReadTimeout" in error.message
            assert "read-idle timeout; configured limit 300s" in error.message
            assert "response_id=failed-response" in error.message
            assert "not automatically retried" in error.message
            assert len(requests) == 2
            assert executions == [{"text": "once"}]

            # Read the on-disk JSONL with a new store and recreate the provider, as after restart.
            reloaded = JsonlSessionStore(tmp_path / "sessions").load(session.session_id)
            persisted_errors = [
                entry.event.payload
                for entry in reloaded.read_entries()
                if isinstance(entry, EventSessionEntry) and entry.event.payload["type"] == "error"
            ]
            assert len(persisted_errors) == 1
            assert persisted_errors[0]["message"] == error.message
            history = reloaded.read_context_messages()
            results = [message for message in history if message.role == "tool"]
            assert len(results) == 1
            assert results[0].tool_call_id == "call-1"
            assert results[0].content == "echo: once"
            assert not results[0].is_error
            failed_replies = [message for message in history if message.finish_reason == "error"]
            assert [message.content for message in failed_replies] == ([partial] if partial else [])

            resumed_agent = CodingSession(
                provider=OpenAICodexProvider(auth_resolver=auth, client=client),
                sessions=sessions,
                tool_registry=tools,
                trusted=True,
            )
            resumed = [event async for event in resumed_agent.run("continue", session=reloaded)]
            assert isinstance(resumed[-1], AgentCompleted)
            assert resumed[-1].outcome == "completed"
            assert executions == [{"text": "once"}]
            assert len(requests) == 3
            replay = requests[-1]["input"]
            assert isinstance(replay, list)
            assert sum(item.get("type") == "function_call_output" for item in replay) == 1
            assert any(
                item.get("type") == "function_call_output" and item.get("output") == "echo: once"
                for item in replay
            )
            assert not any(item.get("call_id") == "uncommitted" for item in replay)

    anyio.run(scenario)
