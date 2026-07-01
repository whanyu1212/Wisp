"""Minimal agent loop for the first Wisp milestone."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from wisp.agent.messages import Message
from wisp.agent.prompt import DEFAULT_CONTEXT_MAX_CHARS, build_prompt_messages
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
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy


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
        tool_policy: ToolPolicy | None = None,
        prompt_messages: Sequence[Message] | None = None,
        project_context_max_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
        max_tool_iterations: int = 8,
    ) -> None:
        self.provider = provider
        self.sessions = sessions
        self.events = events
        self.model = model
        self.tool_registry = tool_registry
        self.tool_policy = tool_policy or ToolPolicy.allow_all_tools()
        self.tool_context = tool_context or ToolContext.default()
        self.tools = (
            tuple(tools)
            if tools is not None
            else self._allowed_tool_specs(tool_registry)
            if tool_registry
            else ()
        )
        self.prompt_messages = tuple(prompt_messages) if prompt_messages is not None else None
        self.project_context_max_chars = project_context_max_chars
        self.max_tool_iterations = max_tool_iterations

    async def run(
        self,
        prompt: str,
        *,
        session: JsonlSession | None = None,
        history: Sequence[Message] = (),
    ) -> AsyncIterator[WispEvent]:
        session = session or self.sessions.create()
        yield await self._emit(AgentStarted(session_id=session.session_id))

        prompt_messages = self._prompt_messages()
        for prompt_message in prompt_messages:
            await session.append_message(prompt_message)

        user_message = Message(role="user", content=prompt)
        await session.append_message(user_message)

        messages: list[Message] = [
            *prompt_messages,
            *self._conversation_history(history),
            user_message,
        ]
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
                        if provider_event.response_id is not None:
                            previous_response_id = provider_event.response_id

                if not tool_calls:
                    break
                if tool_iterations >= self.max_tool_iterations:
                    msg = f"Maximum tool iterations exceeded: {self.max_tool_iterations}"
                    raise RuntimeError(msg)

                tool_iterations += 1
                tool_results: list[ToolCallResult] = []
                for tool_call in tool_calls:
                    arguments = dict(tool_call.arguments)
                    yield await self._emit(
                        ToolExecutionStarted(
                            call_id=tool_call.call_id,
                            name=tool_call.name,
                            arguments=arguments,
                        )
                    )
                    yield await self._emit(
                        ToolCallRequested(
                            call_id=tool_call.call_id,
                            name=tool_call.name,
                            arguments=arguments,
                        )
                    )
                    result = await self._execute_tool_call(tool_call, arguments=arguments)
                    yield await self._emit(
                        ToolExecutionEnded(
                            call_id=tool_call.call_id,
                            name=tool_call.name,
                            output=result.output,
                            is_error=result.is_error,
                        )
                    )
                    yield await self._emit(
                        ToolResultReady(
                            call_id=tool_call.call_id,
                            name=tool_call.name,
                            output=result.output,
                            is_error=result.is_error,
                        )
                    )
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
        self,
        tool_call: ToolCall,
        *,
        arguments: dict[str, object],
    ) -> ToolCallResult:
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
                if not self.tool_policy.allows(tool):
                    output = self.tool_policy.block_reason(tool)
                    is_error = True
                else:
                    result = await tool.run(arguments, self.tool_context)
                    output = result.text
            except UnknownToolError as exc:
                output = str(exc)
                is_error = True
            except Exception as exc:  # noqa: BLE001 - model-visible tool failures should not crash the loop
                output = str(exc)
                is_error = True

        return ToolCallResult(call_id=tool_call.call_id, output=output, is_error=is_error)

    def _allowed_tool_specs(self, tool_registry: ToolRegistry) -> tuple[ToolSpec, ...]:
        return tuple(
            ToolSpec.from_tool(tool)
            for tool in tool_registry.all()
            if self.tool_policy.allows(tool)
        )

    def _prompt_messages(self) -> tuple[Message, ...]:
        if self.prompt_messages is not None:
            return self.prompt_messages
        return build_prompt_messages(
            cwd=self.tool_context.cwd,
            tools=self.tools,
            max_context_chars=self.project_context_max_chars,
        )

    def _conversation_history(self, history: Sequence[Message]) -> tuple[Message, ...]:
        return tuple(message for message in history if message.role not in {"system", "tool"})

    async def _emit(self, event: WispEvent) -> WispEvent:
        if self.events is not None:
            await self.events.emit(event)
        return event
