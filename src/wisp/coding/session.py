"""Application-facing coding session built on the portable agent harness."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from wisp.agent.harness import AgentHarness, AgentHarnessConfig
from wisp.agent.messages import Message, historical_tool_observation
from wisp.agent.prompt import (
    DEFAULT_CONTEXT_MAX_CHARS,
    build_prompt_messages,
    resolve_project_context_root,
)
from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import (
    AgentCompleted,
    AgentStarted,
    ErrorEvent,
    MessageCompleted,
    SessionSaved,
    ToolResultReady,
    TurnCompleted,
    TurnStarted,
    WispEvent,
)
from wisp.providers.base import Provider, ToolSpec
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy

PERSISTED_SESSION_EVENT_TYPES = frozenset(
    {
        "tool.execution.started",
        "tool.call",
        "tool.approval.requested",
        "tool.approval.resolved",
        "tool.execution.ended",
        "error",
    }
)


class CodingSession:
    """Coordinate coding policy, persistence, and events around `AgentHarness`."""

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
        tool_approval_policy: ToolApprovalPolicy | None = None,
        prompt_messages: Sequence[Message] | None = None,
        project_context_max_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
        project_context_root: Path | None = None,
        max_tool_iterations: int | None = None,
        trusted: bool = False,
    ) -> None:
        self.provider = provider
        self.sessions = sessions
        self.events = events
        self.model = model
        self.tool_registry = tool_registry
        self.tool_policy = tool_policy or ToolPolicy.allow_all_tools()
        self.tool_approval_policy = tool_approval_policy or ToolApprovalPolicy.require_approval()
        self.tool_context = tool_context or ToolContext.default()
        self.trusted = trusted
        self.tools = (
            tuple(tools)
            if tools is not None
            else self._allowed_tool_specs(tool_registry)
            if tool_registry
            else ()
        )
        self.prompt_messages = tuple(prompt_messages) if prompt_messages is not None else None
        self.project_context_max_chars = project_context_max_chars
        self.project_context_root = project_context_root
        self.max_tool_iterations = max_tool_iterations

    async def run(
        self,
        prompt: str,
        *,
        session: JsonlSession | None = None,
        history: Sequence[Message] = (),
    ) -> AsyncIterator[WispEvent]:
        session = session or self.sessions.create()

        async def emit(event: WispEvent) -> WispEvent:
            return await self._emit(event, session=session)

        yield await emit(AgentStarted(session_id=session.session_id))

        prompt_messages = self._prompt_messages()
        for prompt_message in prompt_messages:
            await session.append_message(prompt_message)

        user_message = Message(role="user", content=prompt)
        await session.append_message(user_message)
        executor = ConfiguredToolExecutor(
            registry=self.tool_registry,
            context=self.tool_context,
            policy=self.tool_policy,
            approval_policy=self.tool_approval_policy,
        )
        harness = AgentHarness(
            AgentHarnessConfig(
                provider=self.provider,
                tool_executor=executor,
                model=self.model,
                tools=self.tools,
                max_tool_iterations=self.max_tool_iterations,
            ),
            messages=(*prompt_messages, *self._conversation_history(history)),
        )
        assistant_chunks: list[str] = []
        turns = 0
        saw_loop_error = False

        try:
            async for event in harness.prompt_message(user_message):
                if isinstance(event, TurnStarted):
                    turns = event.turn
                elif isinstance(event, MessageCompleted):
                    assistant_chunks.append(event.content)
                elif isinstance(event, ErrorEvent):
                    saw_loop_error = True

                yield await emit(event)

                if isinstance(event, ToolResultReady):
                    await session.append_message(
                        Message(
                            role="tool",
                            content=event.output,
                            tool_call_id=event.call_id,
                            tool_name=event.name,
                        )
                    )
        except Exception as exc:
            if not saw_loop_error:
                yield await emit(ErrorEvent(message=str(exc)))
                if turns > 0:
                    yield await emit(
                        TurnCompleted(turn=turns, outcome="failed", finish_reason="error")
                    )
            yield await emit(
                AgentCompleted(session_id=session.session_id, turns=turns, outcome="failed")
            )
            raise

        assistant_message = Message(role="assistant", content="".join(assistant_chunks))
        await session.append_message(assistant_message)
        yield await emit(SessionSaved(session_id=session.session_id, path=session.path))
        yield await emit(
            AgentCompleted(session_id=session.session_id, turns=turns, outcome="completed")
        )

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
            include_project_context=self.trusted,
            protected_paths=self.tool_context.protected_paths,
            trusted_context_root=self.project_context_root
            or resolve_project_context_root(self.tool_context.cwd),
        )

    def _conversation_history(self, history: Sequence[Message]) -> tuple[Message, ...]:
        normalized: list[Message] = []
        for message in history:
            if message.role == "system":
                continue
            if message.role == "tool":
                normalized.append(historical_tool_observation(message))
            else:
                normalized.append(message)
        return tuple(normalized)

    async def _emit(
        self,
        event: WispEvent,
        *,
        session: JsonlSession | None = None,
    ) -> WispEvent:
        if session is not None and event.type in PERSISTED_SESSION_EVENT_TYPES:
            await session.append_event(event)
        if self.events is not None:
            await self.events.emit(event)
        return event


__all__ = ["CodingSession", "PERSISTED_SESSION_EVENT_TYPES"]
