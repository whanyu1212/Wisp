"""Minimal agent loop for the first Wisp milestone."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from wisp.agent.messages import Message
from wisp.events import (
    AgentStarted,
    AssistantMessage,
    ErrorEvent,
    SessionSaved,
    TokenDelta,
    ToolCallRequested,
    ToolExecutionEnded,
    ToolExecutionStarted,
    ToolResultReady,
    WispEvent,
)
from wisp.providers.base import Provider, ToolCall, ToolCallResult, ToolSpec
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ToolRegistry, UnknownToolError
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.context import ToolContext


class Agent:
    """Coordinates prompts, provider responses, tool calls, and session persistence."""

    def __init__(
        self,
        *,
        provider: Provider,
        sessions: JsonlSessionStore,
        events: EventBus | None = None,
        model: str | None = None,
        tools: Sequence[ToolSpec] | None = None,
        tool_registry: ToolRegistry | None = None,
        tool_context: ToolContext | None = None,
        max_tool_iterations: int = 8,
    ) -> None:
        self.provider = provider
        self.sessions = sessions
        self.events = events
        self.model = model
        self.tool_registry = tool_registry
        self.tools = (
            tuple(tools) if tools is not None else tool_registry.specs() if tool_registry else ()
        )
        self.tool_context = tool_context or ToolContext.default()
        self.max_tool_iterations = max_tool_iterations

    async def run(self, prompt: str) -> AsyncIterator[WispEvent]:
        session = self.sessions.create()
        yield await self._emit(AgentStarted(session_id=session.session_id))

        user_message = Message(role="user", content=prompt)
        await session.append_message(user_message)

        messages: list[Message] = [user_message]
        chunks: list[str] = []
        pending_tool_results: tuple[ToolCallResult, ...] = ()
        previous_response_id: str | None = None
        tool_iterations = 0

        try:
            while True:
                tool_calls: list[ToolCall] = []
                async for provider_event in self.provider.stream(
                    messages,
                    model=self.model,
                    tools=self.tools,
                    tool_results=pending_tool_results,
                    previous_response_id=previous_response_id,
                ):
                    if isinstance(provider_event, str):
                        chunks.append(provider_event)
                        yield await self._emit(TokenDelta(delta=provider_event))
                    else:
                        tool_calls.append(provider_event)
                        if previous_response_id is None:
                            previous_response_id = provider_event.response_id

                if not tool_calls:
                    break
                if tool_iterations >= self.max_tool_iterations:
                    msg = f"Maximum tool iterations exceeded: {self.max_tool_iterations}"
                    raise RuntimeError(msg)

                tool_iterations += 1
                tool_results: list[ToolCallResult] = []
                for tool_call in tool_calls:
                    result, tool_events = await self._execute_tool_call(tool_call)
                    for tool_event in tool_events:
                        yield tool_event
                    tool_results.append(result)
                    await session.append_message(
                        Message(
                            role="tool",
                            content=result.output,
                            tool_call_id=tool_call.call_id,
                            tool_name=tool_call.name,
                        )
                    )
                pending_tool_results = tuple(tool_results)
        except Exception as exc:
            yield await self._emit(ErrorEvent(message=str(exc)))
            raise

        assistant_content = "".join(chunks)
        assistant_message = Message(role="assistant", content=assistant_content)
        await session.append_message(assistant_message)

        yield await self._emit(AssistantMessage(content=assistant_content))
        yield await self._emit(SessionSaved(session_id=session.session_id, path=session.path))

    async def _execute_tool_call(
        self, tool_call: ToolCall
    ) -> tuple[ToolCallResult, tuple[WispEvent, ...]]:
        emitted_events: list[WispEvent] = []

        async def emit(event: WispEvent) -> None:
            emitted_events.append(await self._emit(event))

        arguments = dict(tool_call.arguments)
        await emit(
            ToolCallRequested(
                call_id=tool_call.call_id,
                name=tool_call.name,
                arguments=arguments,
            )
        )
        await emit(
            ToolExecutionStarted(
                call_id=tool_call.call_id,
                name=tool_call.name,
                arguments=arguments,
            )
        )

        output: str
        is_error = False
        if tool_call.parse_error is not None:
            output = tool_call.parse_error
            is_error = True
        elif self.tool_registry is None:
            output = "Tool execution is not configured"
            is_error = True
        else:
            try:
                tool = self.tool_registry.get(tool_call.name)
                result = await tool.run(arguments, self.tool_context)
                output = result.text
            except UnknownToolError as exc:
                output = str(exc)
                is_error = True
            except Exception as exc:  # noqa: BLE001 - model-visible tool failures should not crash the loop
                output = str(exc)
                is_error = True

        await emit(
            ToolExecutionEnded(
                call_id=tool_call.call_id,
                name=tool_call.name,
                output=output,
                is_error=is_error,
            )
        )
        await emit(
            ToolResultReady(
                call_id=tool_call.call_id,
                name=tool_call.name,
                output=output,
                is_error=is_error,
            )
        )
        return (
            ToolCallResult(call_id=tool_call.call_id, output=output, is_error=is_error),
            tuple(emitted_events),
        )

    async def _emit(self, event: WispEvent) -> WispEvent:
        if self.events is not None:
            await self.events.emit(event)
        return event
