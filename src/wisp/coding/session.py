"""Application-facing coding session built on the portable agent harness."""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import anyio

from wisp.agent.context import build_context_budget, context_fingerprint, estimate_context
from wisp.agent.harness import AgentHarness, AgentHarnessConfig
from wisp.agent.messages import (
    CompactionRecord,
    Message,
    SessionEntry,
    message_from_completion_event,
    provider_history_message,
)
from wisp.agent.prompt import (
    DEFAULT_CONTEXT_MAX_CHARS,
    build_prompt_messages,
    resolve_project_context_root,
)
from wisp.agent.transcript import plan_interrupted_tool_repairs
from wisp.coding.compaction import (
    ManualCompactionPlan,
    NothingToCompactError,
    plan_manual_compaction,
    should_auto_compact,
    summarize_manual_compaction,
)
from wisp.coding.stats import build_session_stats
from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import (
    AgentCompleted,
    AgentStarted,
    CompactionCompleted,
    CompactionReason,
    CompactionStarted,
    ContextBudget,
    ErrorEvent,
    MessageCompleted,
    SessionSaved,
    SessionStats,
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
from wisp.sessions.replay import SessionReplay, replay_session_entries
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


@dataclass(frozen=True, slots=True)
class _ContextObservation:
    provider: str
    model: str | None
    tokens: int
    entry_id: str
    context_fingerprint: str


@dataclass(slots=True)
class _AutoCompactionStatus:
    skip_final_save: bool = False


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
        context_reserve_tokens: int = 16_384,
        auto_compaction_enabled: bool = True,
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
        if context_reserve_tokens < 0:
            raise ValueError("context_reserve_tokens must be non-negative")
        self.context_reserve_tokens = context_reserve_tokens
        self.auto_compaction_enabled = auto_compaction_enabled
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
        self._context_observations: dict[str, _ContextObservation] = {}

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
                context_window=self._context_window(),
                context_reserve_tokens=self.context_reserve_tokens,
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
                completion_entry_id: str | None = None
                if isinstance(event, MessageCompleted | ToolExecutionEnded):
                    completion_entry_id = self._queue_completion(
                        session, event, operation_id=operation_id
                    )
                if (
                    isinstance(event, MessageCompleted)
                    and event.finish_reason not in {"error", "cancelled"}
                    and event.usage is not None
                    and event.usage.total_tokens > 0
                    and not event.tool_calls
                    and completion_entry_id is not None
                ):
                    current_messages = self._normalize_provider_messages(harness.messages)
                    self._context_observations[session.session_id] = _ContextObservation(
                        provider=self.provider.name,
                        model=self.model or self.provider.default_model,
                        tokens=event.usage.total_tokens,
                        entry_id=completion_entry_id,
                        context_fingerprint=context_fingerprint(current_messages, self.tools),
                    )

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

        auto_compaction_saved = False
        auto_compaction_status = _AutoCompactionStatus()
        async for compaction_event in self._maybe_auto_compact(
            session,
            harness,
            status=auto_compaction_status,
        ):
            if isinstance(compaction_event, SessionSaved):
                auto_compaction_saved = True
            yield compaction_event
        if not auto_compaction_saved and not auto_compaction_status.skip_final_save:
            yield await emit(SessionSaved(session_id=session.session_id, path=session.path))
        completed = AgentCompleted(
            session_id=session.session_id,
            turns=turns,
            outcome="completed",
        )
        if auto_compaction_status.skip_final_save:
            yield await self._emit_recoverable_event(completed, session=session)
        else:
            yield await emit(completed)

    async def compact(
        self,
        session: JsonlSession,
        instructions: str | None = None,
    ) -> AsyncIterator[WispEvent]:
        """Summarize an active context prefix and append one durable compaction."""

        async with self._operation_lock:
            replay = await self._prepare_compaction_replay(session)
            plan = plan_manual_compaction(replay)
            async for event in self._compact_locked(
                session,
                plan,
                reason="manual",
                instructions=instructions,
                trigger_budget=None,
                recover_failure=False,
            ):
                yield event

    async def _maybe_auto_compact(
        self,
        session: JsonlSession,
        harness: AgentHarness,
        *,
        status: _AutoCompactionStatus,
    ) -> AsyncIterator[WispEvent]:
        provider_messages = self._normalize_provider_messages(harness.messages)
        estimate = estimate_context(provider_messages, self.tools)
        observation = self._context_observations.get(session.session_id)
        current_fingerprint = context_fingerprint(provider_messages, self.tools)
        observed_tokens = None
        observed_is_current = False
        if observation is not None:
            observed_tokens = observation.tokens
            observed_is_current = (
                observation.provider == self.provider.name
                and observation.model == (self.model or self.provider.default_model)
                and observation.context_fingerprint == current_fingerprint
            )
        budget = build_context_budget(
            estimate,
            context_window=self._context_window(),
            reserve_tokens=self.context_reserve_tokens,
            observed_tokens=observed_tokens,
            observed_is_current=observed_is_current,
        )
        if not should_auto_compact(budget, enabled=self.auto_compaction_enabled):
            return

        replay: SessionReplay | None = None
        try:
            replay = await self._prepare_compaction_replay(session)
            plan = plan_manual_compaction(replay)
        except NothingToCompactError:
            return
        except Exception as exc:
            status.skip_final_save = True
            source_entry_count = len(replay.context_entry_ids) if replay is not None else 0
            yield await self._emit_recoverable_event(
                CompactionStarted(
                    session_id=session.session_id,
                    reason="threshold",
                    source_entry_count=source_entry_count,
                    trigger_budget=budget,
                ),
                session=session,
            )
            yield await self._emit_recoverable_event(
                CompactionCompleted(
                    session_id=session.session_id,
                    reason="threshold",
                    outcome="failed",
                    replaced_entry_count=0,
                    retained_entry_count=source_entry_count,
                    provider=self.provider.name,
                    model=self.model or self.provider.default_model,
                    error=str(exc),
                ),
                session=session,
            )
            return
        async for event in self._compact_locked(
            session,
            plan,
            reason="threshold",
            instructions=None,
            trigger_budget=budget,
            recover_failure=True,
        ):
            yield event

    async def _prepare_compaction_replay(self, session: JsonlSession) -> SessionReplay:
        await self._flush_pending_entries()
        replay = await self._read_context(session)
        repair_plan = plan_interrupted_tool_repairs(replay.messages)
        if repair_plan.repairs:
            for repair in repair_plan.repairs:
                self._queue_message(session, repair)
            await self._flush_pending_entries()
            self._history_refresh_session_ids.add(session.session_id)
            replay = await self._read_context(session)
        return replay

    async def _compact_locked(
        self,
        session: JsonlSession,
        plan: ManualCompactionPlan,
        *,
        reason: CompactionReason,
        instructions: str | None,
        trigger_budget: ContextBudget | None,
        recover_failure: bool,
    ) -> AsyncIterator[WispEvent]:
        started = CompactionStarted(
            session_id=session.session_id,
            reason=reason,
            source_entry_count=len(plan.expected_context_entry_ids),
            trigger_budget=trigger_budget,
        )
        provider_name = self.provider.name
        effective_model = self.model or self.provider.default_model
        try:
            if recover_failure:
                yield await self._emit_recoverable_event(started, session=session)
            else:
                yield await self._emit(started, session=session)
            summary = await summarize_manual_compaction(
                plan,
                provider=self.provider,
                model=self.model,
                effort=self.effort,
                instructions=instructions,
            )
            normalized_instructions = (
                instructions.strip() if instructions is not None and instructions.strip() else None
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
                    reason=reason,
                    trigger_budget=trigger_budget,
                ),
            )
            with anyio.CancelScope(shield=True):
                try:
                    await session.append_compaction_entry(
                        entry,
                        expected_context_entry_ids=plan.expected_context_entry_ids,
                    )
                except Exception:
                    # A write can commit and then fail during post-append validation.
                    # Retrying the stable entry id reconciles that uncertain outcome.
                    await session.append_compaction_entry(
                        entry,
                        expected_context_entry_ids=plan.expected_context_entry_ids,
                    )
                self._history_refresh_session_ids.add(session.session_id)
                self._context_observations.pop(session.session_id, None)
                saved = SessionSaved(session_id=session.session_id, path=session.path)
                publication_errors: list[str] = []
                try:
                    await self._emit(saved, session=session)
                except Exception as exc:
                    publication_errors.append(str(exc))
                completed = CompactionCompleted(
                    session_id=session.session_id,
                    reason=reason,
                    outcome="completed",
                    compaction_id=entry.id,
                    replaced_entry_count=len(plan.replaced_entry_ids),
                    retained_entry_count=len(plan.retained_rows),
                    provider=provider_name,
                    model=effective_model,
                    usage=summary.usage,
                    error=(
                        "Event publication failed: " + "; ".join(publication_errors)
                        if publication_errors and recover_failure
                        else None
                    ),
                )
                try:
                    await self._emit(completed, session=session)
                except Exception as exc:
                    publication_errors.append(str(exc))
                if publication_errors and recover_failure:
                    completed = completed.model_copy(
                        update={
                            "error": "Event publication failed: " + "; ".join(publication_errors)
                        }
                    )
            yield saved
            yield completed
            if publication_errors and not recover_failure:
                yield ErrorEvent(
                    message=(
                        "Compaction committed, but event publication failed: "
                        + "; ".join(publication_errors)
                    )
                )
        except Exception as exc:
            error = str(exc)
            if not recover_failure:
                yield await self._emit(ErrorEvent(message=error), session=session)
            failed = CompactionCompleted(
                session_id=session.session_id,
                reason=reason,
                outcome="failed",
                replaced_entry_count=len(plan.replaced_entry_ids),
                retained_entry_count=len(plan.retained_rows),
                provider=provider_name,
                model=effective_model,
                error=error,
            )
            if recover_failure:
                yield await self._emit_recoverable_event(failed, session=session)
            else:
                yield await self._emit(failed, session=session)
            if not recover_failure:
                raise
        except BaseException as exc:
            if not isinstance(exc, anyio.get_cancelled_exc_class()):
                raise
            with anyio.CancelScope(shield=True):
                cancelled_event = CompactionCompleted(
                    session_id=session.session_id,
                    reason=reason,
                    outcome="cancelled",
                    replaced_entry_count=len(plan.replaced_entry_ids),
                    retained_entry_count=len(plan.retained_rows),
                    provider=provider_name,
                    model=effective_model,
                    error=str(exc) or "Compaction cancelled",
                )
                cancelled: WispEvent
                if recover_failure:
                    cancelled = await self._emit_recoverable_event(cancelled_event, session=session)
                else:
                    cancelled = await self._emit(cancelled_event, session=session)
            yield cancelled
            raise

    async def _emit_recoverable_event(
        self,
        event: WispEvent,
        *,
        session: JsonlSession,
    ) -> WispEvent:
        try:
            return await self._emit(event, session=session)
        except Exception:
            return event

    async def get_session_stats(self, session: JsonlSession | None = None) -> SessionStats:
        """Return a consistent, non-persisted statistics snapshot."""

        async with self._operation_lock:
            await self._flush_pending_entries()
            if session is None or not session.path.exists():
                entries: tuple[SessionEntry, ...] = ()
            else:
                entries = await anyio.to_thread.run_sync(session.read_entries)
            replay = replay_session_entries(entries)
            history = self._conversation_history(replay.messages)
            provider_messages = self._provider_messages(history)
            observation = self._context_observations.get(
                session.session_id if session is not None else ""
            )
            observed_tokens = None
            observed_is_current = False
            observed_entry_id = None
            observed_context_fingerprint = None
            if observation is not None:
                observed_tokens = observation.tokens
                observed_entry_id = observation.entry_id
                observed_context_fingerprint = observation.context_fingerprint
                observed_is_current = (
                    observation.provider == self.provider.name
                    and observation.model == (self.model or self.provider.default_model)
                )
            return build_session_stats(
                session_id=session.session_id if session is not None else None,
                entries=entries,
                replay=replay,
                provider_messages=provider_messages,
                tools=self.tools,
                context_window=self._context_window(),
                reserve_tokens=self.context_reserve_tokens,
                observed_tokens=observed_tokens,
                observed_is_current=observed_is_current,
                observed_entry_id=observed_entry_id,
                observed_context_fingerprint=observed_context_fingerprint,
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

    def _provider_messages(self, history: Sequence[Message]) -> tuple[Message, ...]:
        return (*self._prompt_messages(), *self._normalize_provider_messages(history))

    def _normalize_provider_messages(self, messages: Sequence[Message]) -> tuple[Message, ...]:
        return tuple(
            normalized
            for message in messages
            if (normalized := provider_history_message(message)) is not None
        )

    def _context_window(self) -> int | None:
        if self.models is None:
            return None
        return self.models.context_window(
            self.provider.name,
            self.model,
            default_model=self.provider.default_model,
        )

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
    ) -> str:
        return self._queue_message(
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
    ) -> str:
        entry = SessionEntry(
            session_id=session.session_id,
            message=message,
            operation_id=operation_id,
            created_at=message.created_at,
        )
        self._pending_entries.append(_PendingSessionEntry(session=session, entry=entry))
        return entry.id

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
