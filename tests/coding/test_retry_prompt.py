"""A strict Chat Completions endpoint can resume an interrupted session."""

import json
from itertools import pairwise
from pathlib import Path

import anyio
import httpx
import pytest
from openai import AsyncOpenAI

from wisp.coding.session import CodingSession
from wisp.events import AgentCompleted
from wisp.providers.openai_compatible import OpenAICompatibleProvider
from wisp.sessions.jsonl import JsonlSessionStore


@pytest.mark.parametrize("cancel", [False, True], ids=["failed", "cancelled"])
def test_next_prompt_recovers(tmp_path: Path, cancel: bool) -> None:
    requests: list[list[str]] = []

    async def endpoint(request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)["messages"]
        requests.append([m["content"] for m in messages if m["role"] == "user"])
        if len(requests) == 1:
            if cancel:
                assert agent.cancel_run()
            return httpx.Response(400, json={"error": {"message": "interrupted"}})
        if any(a["role"] == b["role"] == "user" for a, b in pairwise(messages)):
            return httpx.Response(400, json={"error": {"message": "roles must alternate"}})
        chunk = {
            "id": "ok",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test",
            "choices": [{"index": 0, "delta": {"content": "recovered"}, "finish_reason": "stop"}],
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
        )

    store = JsonlSessionStore(tmp_path)
    session = store.create()
    http = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
    client = AsyncOpenAI(base_url="http://mock.invalid/v1", api_key="test", http_client=http)
    agent = CodingSession(
        provider=OpenAICompatibleProvider(
            client=client, base_url="http://mock.invalid/v1", default_model="test"
        ),
        sessions=store,
    )

    async def run() -> None:
        async with client:
            for prompt in ("first", "retry"):
                outcomes = [
                    event.outcome
                    async for event in agent.run(prompt, session=session)
                    if isinstance(event, AgentCompleted)
                ]
                if prompt == "first" and cancel:
                    assert outcomes == ["cancelled"]

    anyio.run(run)
    assert requests == [["first"], ["first\n\nretry"]]
    assert [
        (m.role, m.content) for m in session.read_messages() if m.role in {"user", "assistant"}
    ] == [("user", "first"), ("user", "retry"), ("assistant", "recovered")]
