"""Fake provider used to prove the agent loop before real SDKs exist."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import anyio

from wisp.agent.messages import Message
from wisp.providers.base import ProviderStreamEvent, ToolCallResult, ToolSpec


class FakeProvider:
    """Deterministic provider for tests and early CLI smoke runs."""

    name = "fake"
    default_model: str | None = "fake"

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        prompt = _last_user_prompt(messages)
        response = f"fake response to: {prompt}"

        for index, word in enumerate(response.split(" ")):
            await anyio.sleep(0)
            yield word if index == 0 else f" {word}"


def _last_user_prompt(messages: Sequence[Message]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            return message.content
    return ""
