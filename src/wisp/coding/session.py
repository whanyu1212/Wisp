"""Application-facing coding session built on the portable agent harness."""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import anyio

from wisp.agent.harness import AgentHarness, AgentHarnessConfig
from wisp.agent.messages import (
    CompactionRecord,
    Message,
    SessionEntry,
    message_from_completion_event,
)
from wisp.agent.prompt import (
    DEFAULT_CONTEXT_MAX_CHARS,
    build_prompt_messages,
    resolve_project_context_root,
)
from wisp.agent.transcript import plan_interrupted_tool_repairs
from wisp.coding.compaction import plan_manual_compaction, summarize_manual_compaction
from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import (
    AgentCompleted,
    AgentStarted,
    CompactionCompleted,
    CompactionStarted,
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
from wisp.sessions.replay import SessionReplay
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
        self._operation_lock = anyio.Semaphore(1)
        self._history_refresh_session_ids: set[str] = set()

    async def run(
        self,
        prompt: str,
        *,
        session: JsonlSession | None = None,
        history: Sequence[Message] = (),
        operation_id: str | None = None,
    ) -> AsyncIterator[WispEvent]:
        async with self._operation_lock:
            events = self._run(
                prompt,
                session=session,
                history=history,
                operation_id=operation_id,
            )
            try:
                async for event in events:
                    yield event
            finally:
                await events.aclose()

    async def _run(
        self,
        prompt: str,
        *,
        session: JsonlSession | None = None,
        history: Sequence[Message] = (),
        operation_id: str | None = None,
    ) -> AsyncGenerator[WispEvent, None]:
        session = session or self.sessions.create()
        recover_history = session.session_id in self._history_refresh_session_ids or any(
            pending.entry.session_id == session.session_id for pending in self._pending_entries
        )
        await self._flush_pending_entries()
        if recover_history:
            history = await anyio.to_thread.run_sync(session.read_context_messages)
            self._history_refresh_session_ids.discard(session.session_id)

        async def emit(event: WispEvent) -> WispEvent:
            return await self._emit(event, session=session, operation_id=operation_id)

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
            await session.append_message(prompt_message, operation_id=operation_id)

        user_message = Message(role="user", content=prompt)
        await session.append_message(user_message, operation_id=operation_id)
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
                    self._queue_completion(session, event, operation_id=operation_id)

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
                    await self._repair_and_flush(
                        session,
                        harness,
                        operation_id=operation_id,
                    )

        yield await emit(SessionSaved(session_id=session.session_id, path=session.path))
        yield await emit(
            AgentCompleted(session_id=session.session_id, turns=turns, outcome="completed")
        )

    async def compact(
        self,
        session: JsonlSession,
        instructions: str | None = None,
    ) -> AsyncIterator[WispEvent]:
        """Summarize an active context prefix and append one durable compaction."""

        async with self._operation_lock:
            await self._flush_pending_entries()
            replay = await self._read_context(session)
            repair_plan = plan_interrupted_tool_repairs(replay.messages)
            if repair_plan.repairs:
                for repair in repair_plan.repairs:
                    self._queue_message(session, repair)
                await self._flush_pending_entries()
                self._history_refresh_session_ids.add(session.session_id)
                replay = await self._read_context(session)

            plan = plan_manual_compaction(replay)
            started = CompactionStarted(
                session_id=session.session_id,
                source_entry_count=len(plan.expected_context_entry_ids),
            )
            provider_name = self.provider.name
            effective_model = self.model or self.provider.default_model
            try:
                yield await self._emit(started, session=session)
                summary = await summarize_manual_compaction(
                    plan,
                    provider=self.provider,
                    model=self.model,
                    effort=self.effort,
                    instructions=instructions,
                )
                normalized_instructions = (
                    instructions.strip()
                    if instructions is not None and instructions.strip()
                    else None
                )
                entry = SessionEntry(
                    session_id=session.session_id,
                    kind="compaction",
                    compaction=CompactionRecord(
                        summary=summary.summary,
                        replaced_entry_ids=plan.replaced_entry_ids,
                        provider=provider_name,
                        model=effective_model,
                        instructions=normalized_instructions,
                        usage=summary.usage,
                    ),
                )
                with anyio.CancelScope(shield=True):
                    await session.append_compaction_entry(
                        entry,
                        expected_context_entry_ids=plan.expected_context_entry_ids,
                    )
                    self._history_refresh_session_ids.add(session.session_id)
                    saved = SessionSaved(session_id=session.session_id, path=session.path)
                    publication_errors: list[str] = []
                    try:
                        await self._emit(saved, session=session)
                    except Exception as exc:
                        publication_errors.append(str(exc))
                    completed = CompactionCompleted(
                        session_id=session.session_id,
                        outcome="completed",
                        compaction_id=entry.id,
                        replaced_entry_count=len(plan.replaced_entry_ids),
                        retained_entry_count=len(plan.retained_rows),
                        provider=provider_name,
                        model=effective_model,
                        usage=summary.usage,
                    )
                    try:
                        await self._emit(completed, session=session)
                    except Exception as exc:
                        publication_errors.append(str(exc))
                yield saved
                yield completed
                if publication_errors:
                    yield ErrorEvent(
                        message=(
                            "Compaction committed, but event publication failed: "
                            + "; ".join(publication_errors)
                        )
                    )
            except Exception as exc:
                error = str(exc)
                yield await self._emit(ErrorEvent(message=error), session=session)
                yield await self._emit(
                    CompactionCompleted(
                        session_id=session.session_id,
                        outcome="failed",
                        replaced_entry_count=len(plan.replaced_entry_ids),
                        retained_entry_count=len(plan.retained_rows),
                        provider=provider_name,
                        model=effective_model,
                        error=error,
                    ),
                    session=session,
                )
                raise
            except BaseException as exc:
                if not isinstance(exc, anyio.get_cancelled_exc_class()):
                    raise
                with anyio.CancelScope(shield=True):
                    cancelled = await self._emit(
                        CompactionCompleted(
                            session_id=session.session_id,
                            outcome="cancelled",
                            replaced_entry_count=len(plan.replaced_entry_ids),
                            retained_entry_count=len(plan.retained_rows),
                            provider=provider_name,
                            model=effective_model,
                            error=str(exc) or "Compaction cancelled",
                        ),
                        session=session,
                    )
                yield cancelled
                raise

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
        operation_id: str | None = None,
    ) -> WispEvent:
        await self._flush_pending_entries()
        if session is not None and event.type in PERSISTED_SESSION_EVENT_TYPES:
            await session.append_event(event, operation_id=operation_id)
        if self.events is not None:
            await self.events.emit(event)
        return event

    def _queue_completion(
        self,
        session: JsonlSession,
        event: MessageCompleted | ToolExecutionEnded,
        *,
        operation_id: str | None = None,
    ) -> None:
        self._queue_message(
            session,
            message_from_completion_event(event),
            operation_id=operation_id,
        )

    def _queue_message(
        self,
        session: JsonlSession,
        message: Message,
        *,
        operation_id: str | None = None,
    ) -> None:
        entry = SessionEntry(
            session_id=session.session_id,
            message=message,
            operation_id=operation_id,
            created_at=message.created_at,
        )
        self._pending_entries.append(_PendingSessionEntry(session=session, entry=entry))

    async def _repair_and_flush(
        self,
        session: JsonlSession,
        harness: AgentHarness,
        *,
        operation_id: str | None = None,
    ) -> None:
        """Persist synthetic repairs before the transcript crosses a new boundary."""

        for message in harness.repair_interrupted_tool_calls():
            self._queue_message(session, message, operation_id=operation_id)
        await self._flush_pending_entries()

    async def _read_context(self, session: JsonlSession) -> SessionReplay:
        if not session.path.exists():
            return SessionReplay(rows=())
        return await anyio.to_thread.run_sync(session.read_context)

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
