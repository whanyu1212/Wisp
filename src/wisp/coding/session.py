"""Application-facing coding session built on the portable agent harness."""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import anyio

from wisp.agent.harness import AgentHarness, AgentHarnessConfig
from wisp.agent.messages import (
    Message,
    SessionEntry,
    message_from_completion_event,
)
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
    ToolExecutionEnded,
    TurnCompleted,
    TurnStarted,
    WispEvent,
)
from wisp.providers.base import Provider, ToolSpec
from wisp.providers.catalog import ModelRegistry
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
        "context.pressure",
        "context.overflow",
        "error",
    }
)


@dataclass(frozen=True, slots=True)
class _PendingSessionEntry:
    session: JsonlSession
    entry: SessionEntry


class CodingSession:
    """Coordinate coding policy, persistence, and events around `AgentHarness`."""

    def __init__(
        self,
        *,
        provider: Provider,
        sessions: JsonlSessionStore,
        events: EventBus | None = None,
        model: str | None = None,
        effort: str | None = None,
        models: ModelRegistry | None = None,
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
        self.effort = effort
        self.models = models
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
        self._pending_entries: deque[_PendingSessionEntry] = deque()
        self._pending_flush_lock = anyio.Lock()
        self._history_refresh_session_ids: set[str] = set()

    async def run(
        self,
        prompt: str,
        *,
        session: JsonlSession | None = None,
        history: Sequence[Message] = (),
    ) -> AsyncIterator[WispEvent]:
        session = session or self.sessions.create()
        recover_history = session.session_id in self._history_refresh_session_ids or any(
            pending.entry.session_id == session.session_id for pending in self._pending_entries
        )
        await self._flush_pending_entries()
        if recover_history:
            history = await anyio.to_thread.run_sync(session.read_messages)
            self._history_refresh_session_ids.discard(session.session_id)

        async def emit(event: WispEvent) -> WispEvent:
            return await self._emit(event, session=session)

        prompt_messages = self._prompt_messages()
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
                effort=self.effort,
                context_window=(
                    self.models.context_window(
                        self.provider.name,
                        self.model,
                        default_model=self.provider.default_model,
                    )
                    if self.models is not None
                    else None
                ),
            ),
            messages=(*prompt_messages, *self._conversation_history(history)),
        )
        await self._repair_and_flush(session, harness)

        yield await emit(AgentStarted(session_id=session.session_id))

        for prompt_message in prompt_messages:
            await session.append_message(prompt_message)

        user_message = Message(role="user", content=prompt)
        await session.append_message(user_message)
        turns = 0
        saw_loop_error = False
        harness_events = harness.prompt_message(user_message)

        try:
            async for event in harness_events:
                if isinstance(event, TurnStarted):
                    turns = event.turn
                elif isinstance(event, ErrorEvent):
                    saw_loop_error = True
                if isinstance(event, MessageCompleted | ToolExecutionEnded):
                    self._queue_completion(session, event)

                yield await emit(event)
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
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await harness_events.aclose()
                finally:
                    await self._repair_and_flush(session, harness)

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
        """Retain raw tool metadata while replacing stale system prompts."""

        return tuple(message for message in history if message.role != "system")

    async def _emit(
        self,
        event: WispEvent,
        *,
        session: JsonlSession | None = None,
    ) -> WispEvent:
        await self._flush_pending_entries()
        if session is not None and event.type in PERSISTED_SESSION_EVENT_TYPES:
            await session.append_event(event)
        if self.events is not None:
            await self.events.emit(event)
        return event

    def _queue_completion(
        self,
        session: JsonlSession,
        event: MessageCompleted | ToolExecutionEnded,
    ) -> None:
        self._queue_message(session, message_from_completion_event(event))

    def _queue_message(self, session: JsonlSession, message: Message) -> None:
        entry = SessionEntry(
            session_id=session.session_id,
            message=message,
            created_at=message.created_at,
        )
        self._pending_entries.append(_PendingSessionEntry(session=session, entry=entry))

    async def _repair_and_flush(
        self,
        session: JsonlSession,
        harness: AgentHarness,
    ) -> None:
        """Persist synthetic repairs before the transcript crosses a new boundary."""

        for message in harness.repair_interrupted_tool_calls():
            self._queue_message(session, message)
        await self._flush_pending_entries()

    async def _flush_pending_entries(self) -> None:
        async with self._pending_flush_lock:
            while self._pending_entries:
                pending = self._pending_entries[0]
                try:
                    await pending.session.append_entry(pending.entry)
                except BaseException:
                    self._history_refresh_session_ids.add(pending.entry.session_id)
                    raise
                self._pending_entries.popleft()


__all__ = ["CodingSession", "PERSISTED_SESSION_EVENT_TYPES"]
