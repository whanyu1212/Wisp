"""Minimal agent loop for the first Wisp milestone."""

from __future__ import annotations

from collections.abc import AsyncIterator

from wisp.agent.messages import Message
from wisp.events import (
    AgentStarted,
    AssistantMessage,
    ErrorEvent,
    SessionSaved,
    TokenDelta,
    WispEvent,
)
from wisp.providers.base import Provider
from wisp.runtime.event_bus import EventBus
from wisp.sessions.jsonl import JsonlSessionStore


class Agent:
    """Coordinates one prompt, one provider response, and session persistence."""

    def __init__(
        self,
        *,
        provider: Provider,
        sessions: JsonlSessionStore,
        events: EventBus | None = None,
    ) -> None:
        self.provider = provider
        self.sessions = sessions
        self.events = events

    async def run(self, prompt: str) -> AsyncIterator[WispEvent]:
        session = self.sessions.create()
        yield await self._emit(AgentStarted(session_id=session.session_id))

        user_message = Message(role="user", content=prompt)
        await session.append_message(user_message)

        messages: list[Message] = [user_message]
        chunks: list[str] = []

        try:
            async for delta in self.provider.stream(messages):
                chunks.append(delta)
                yield await self._emit(TokenDelta(delta=delta))
        except Exception as exc:
            yield await self._emit(ErrorEvent(message=str(exc)))
            raise

        assistant_content = "".join(chunks)
        assistant_message = Message(role="assistant", content=assistant_content)
        await session.append_message(assistant_message)

        yield await self._emit(AssistantMessage(content=assistant_content))
        yield await self._emit(SessionSaved(session_id=session.session_id, path=session.path))

    async def _emit(self, event: WispEvent) -> WispEvent:
        if self.events is not None:
            await self.events.emit(event)
        return event
