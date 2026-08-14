"""Application-facing coding session built on the portable agent harness."""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import anyio

from wisp.agent.configuration import (
    validate_non_negative_integer,
    validate_optional_non_negative_integer,
)
from wisp.agent.context import build_context_budget, context_fingerprint, estimate_context
from wisp.agent.harness import AgentHarness, AgentHarnessConfig, QueuedMessages
from wisp.agent.messages import (
    CompactionRecord,
    Message,
    message_from_completion_event,
    provider_history_message,
)
from wisp.agent.mode import DEFAULT_AGENT_MODE, PLAN_MODE_SYSTEM_PROMPT, AgentMode
from wisp.agent.prompt import (
    DEFAULT_CONTEXT_MAX_CHARS,
    build_prompt_messages,
    resolve_project_context_root,
)
from wisp.agent.transcript import plan_interrupted_tool_repairs
from wisp.coding.compaction import (
    CompactionSummary,
    CompactionSummaryError,
    ManualCompactionPlan,
    NothingToCompactError,
    plan_manual_compaction,
    plan_preflight_compaction,
    should_auto_compact,
    summarize_manual_compaction,
    truncate_active_turn_tool_results,
)
from wisp.coding.configuration import CodingSessionConfiguration
from wisp.coding.costs import CostEstimator
from wisp.coding.stats import build_session_stats
from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import (
    AgentCompleted,
    AgentStarted,
    CodingSessionState,
    CompactionCompleted,
    CompactionReason,
    CompactionStarted,
    ContextBudget,
    ContextEstimated,
    ErrorEvent,
    MessageCompleted,
    MessageDelta,
    QueueKind,
    QueueMessageInjected,
    QueueMode,
    QueueUpdated,
    SessionSaved,
    SessionStats,
    SkillInvoked,
    ToolApprovalResolved,
    ToolExecutionEnded,
    ToolPresentationStatus,
    TurnCompleted,
    TurnStarted,
    WispEvent,
)
from wisp.providers.base import ContextOverflowError, Provider, ToolSpec
from wisp.providers.catalog import ModelRegistry
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ToolRegistry, UnknownToolError
from wisp.sessions.entries import (
    CompactionSessionEntry,
    EventSessionEntry,
    MessageSessionEntry,
    PersistedEventEnvelope,
    SessionEntry,
    ToolResultPresentationSnapshot,
    is_session_tree_entry,
)
from wisp.sessions.errors import StaleSessionWriterError
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore
from wisp.sessions.replay import SessionReplay, replay_session_entries
from wisp.skills.invocation import expand_skill_invocation
from wisp.skills.models import SkillCatalog
from wisp.skills.prompt import build_skill_index
from wisp.skills.tool import SkillTool
from wisp.tool_presentation import tool_result_status
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


@dataclass(slots=True)
class _RunPersistence:
    """Advance one run only along the session branch it has observed."""

    session: JsonlSession
    expected_active_leaf_id: str | None
    operation_id: str | None

    async def append_entry(self, entry: SessionEntry) -> SessionEntry:
        persisted = await self.session.append_entry_if_current(
            entry,
            expected_active_leaf_id=self.expected_active_leaf_id,
        )
        if is_session_tree_entry(persisted):
            self.expected_active_leaf_id = persisted.id
        return persisted

    async def append_message(self, message: Message) -> SessionEntry:
        persisted = await self.session.append_message_if_current(
            message,
            expected_active_leaf_id=self.expected_active_leaf_id,
            operation_id=self.operation_id,
        )
        self.expected_active_leaf_id = persisted.id
        return persisted

    async def append_event(self, event: WispEvent) -> SessionEntry:
        persisted = await self.append_entry(
            EventSessionEntry(
                session_id=self.session.session_id,
                event=PersistedEventEnvelope(payload=event.model_dump(mode="json")),
                operation_id=self.operation_id,
            )
        )
        return persisted

    async def append_compaction(
        self,
        entry: CompactionSessionEntry,
        *,
        expected_context_entry_ids: Sequence[str],
    ) -> SessionEntry:
        persisted = await self.session.append_compaction_entry_if_current(
            entry,
            expected_context_entry_ids=expected_context_entry_ids,
            expected_active_leaf_id=self.expected_active_leaf_id,
        )
        self.expected_active_leaf_id = persisted.id
        return persisted


@dataclass(frozen=True, slots=True)
class _PendingSessionEntry:
    persistence: _RunPersistence
    entry: SessionEntry


@dataclass(frozen=True, slots=True)
class _RetainedQueueState:
    messages: QueuedMessages
    steering_mode: QueueMode = "one_at_a_time"
    follow_up_mode: QueueMode = "one_at_a_time"


def _retained_queue_state(harness: AgentHarness) -> _RetainedQueueState:
    config = harness.config
    return _RetainedQueueState(
        messages=harness.queued_messages,
        steering_mode=config.steering_mode,
        follow_up_mode=config.follow_up_mode,
    )


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
        skill_catalog: SkillCatalog | None = None,
        context_reserve_tokens: int = 16_384,
        auto_compaction_enabled: bool = True,
        mode: AgentMode = DEFAULT_AGENT_MODE,
    ) -> None:
        self.sessions = sessions
        self.events = events
        self.tool_registry = tool_registry
        self.tool_policy = tool_policy or ToolPolicy.allow_all_tools()
        self.tool_approval_policy = tool_approval_policy or ToolApprovalPolicy.require_approval()
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
        validate_optional_non_negative_integer(max_tool_iterations, field="max_tool_iterations")
        self.max_tool_iterations = max_tool_iterations
        self.mode = mode
        self._pending_entries: deque[_PendingSessionEntry] = deque()
        self._pending_flush_lock = anyio.Lock()
        self._operation_lock = anyio.Semaphore(1)
        self._history_refresh_session_ids: set[str] = set()
        self._context_observations: dict[str, _ContextObservation] = {}
        self._operation_active = False
        self._active_harness: AgentHarness | None = None
        self._active_session_id: str | None = None
        self._active_persistence: _RunPersistence | None = None
        self._last_session_id: str | None = None
        self._accepting_queued_messages = False
        self._retained_queues: dict[str, _RetainedQueueState] = {}
        self._apply_configuration(
            CodingSessionConfiguration(
                provider=provider,
                model=model,
                effort=effort,
                models=models,
                tool_context=tool_context or ToolContext.default(),
                skill_catalog=skill_catalog or SkillCatalog(),
                trusted=trusted,
                context_reserve_tokens=context_reserve_tokens,
                auto_compaction_enabled=auto_compaction_enabled,
            )
        )

    @classmethod
    def from_configuration(
        cls,
        configuration: CodingSessionConfiguration,
        *,
        sessions: JsonlSessionStore,
        events: EventBus | None = None,
        tools: Sequence[ToolSpec] | None = None,
        tool_registry: ToolRegistry | None = None,
        tool_policy: ToolPolicy | None = None,
        tool_approval_policy: ToolApprovalPolicy | None = None,
        prompt_messages: Sequence[Message] | None = None,
        project_context_max_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
        project_context_root: Path | None = None,
        max_tool_iterations: int | None = None,
    ) -> CodingSession:
        """Create a session from one resolved dynamic configuration snapshot."""

        return cls(
            provider=configuration.provider,
            sessions=sessions,
            events=events,
            model=configuration.model,
            effort=configuration.effort,
            models=configuration.models,
            tools=tools,
            tool_registry=tool_registry,
            tool_context=configuration.tool_context,
            tool_policy=tool_policy,
            tool_approval_policy=tool_approval_policy,
            prompt_messages=prompt_messages,
            project_context_max_chars=project_context_max_chars,
            project_context_root=project_context_root,
            max_tool_iterations=max_tool_iterations,
            trusted=configuration.trusted,
            skill_catalog=configuration.skill_catalog,
            context_reserve_tokens=configuration.context_reserve_tokens,
            auto_compaction_enabled=configuration.auto_compaction_enabled,
        )

    @property
    def configuration(self) -> CodingSessionConfiguration:
        """Return the configuration applied to subsequent operations."""

        return CodingSessionConfiguration(
            provider=self.provider,
            model=self.model,
            effort=self.effort,
            models=self.models,
            tool_context=self.tool_context,
            skill_catalog=self.skill_catalog,
            trusted=self.trusted,
            context_reserve_tokens=self.context_reserve_tokens,
            auto_compaction_enabled=self.auto_compaction_enabled,
        )

    def reconfigure(self, configuration: CodingSessionConfiguration) -> None:
        """Apply a complete configuration between prompt or compaction operations."""

        if self._operation_active:
            raise RuntimeError("CodingSession is busy")
        self._apply_configuration(configuration)

    def set_mode(self, mode: AgentMode) -> None:
        """Select the operating mode for subsequent agent operations."""

        if self._operation_active:
            raise RuntimeError("CodingSession is busy")
        self.mode = mode

    def reset_session_state(self) -> None:
        """Discard process-local queue state when no persisted session is selected."""

        if self._operation_active:
            raise RuntimeError("CodingSession is busy")
        self._last_session_id = None
        self._retained_queues.clear()

    async def follow_up(self, content: str) -> QueueUpdated:
        """Queue user text for the active run's next completed-turn boundary."""

        harness = self._active_queue_harness()
        message = await self._prepare_user_message(content)
        if harness is not self._active_queue_harness():
            raise RuntimeError("CodingSession active agent run changed")
        return harness.follow_up_message(message)

    async def steer(self, content: str) -> QueueUpdated:
        """Queue user text after the active run's current assistant/tool batch."""

        harness = self._active_queue_harness()
        message = await self._prepare_user_message(content)
        if harness is not self._active_queue_harness():
            raise RuntimeError("CodingSession active agent run changed")
        return harness.steer_message(message)

    def queue_state(self, session: JsonlSession | None = None) -> QueueUpdated:
        """Return the current or retained queue state without requiring an active run."""

        harness = self._active_harness
        if harness is not None and (
            session is None or session.session_id == self._active_session_id
        ):
            return harness.queue_updated_event()

        session_id = session.session_id if session is not None else self._last_session_id
        if session_id is None:
            return QueueUpdated()
        return self._queue_updated_from_snapshot(self._retained_queues.get(session_id))

    def state_snapshot(self, session: JsonlSession | None = None) -> CodingSessionState:
        """Return an immediate configuration and queue summary without I/O."""

        queue = self.queue_state(session)
        return CodingSessionState(
            provider=self.provider.name,
            model=self.model or self.provider.default_model,
            mode=self.mode,
            effort=self.effort,
            auto_compaction_enabled=self.auto_compaction_enabled,
            steering_mode=queue.steering_mode,
            follow_up_mode=queue.follow_up_mode,
            pending_steering_count=len(queue.steering),
            pending_follow_up_count=len(queue.follow_up),
        )

    def set_queue_mode(self, kind: QueueKind, mode: QueueMode) -> QueueUpdated:
        """Set one active queue's drain mode."""

        harness = self._active_queue_harness()
        if kind == "steering":
            return harness.set_steering_mode(mode)
        if kind == "follow_up":
            return harness.set_follow_up_mode(mode)
        raise ValueError(f"Unsupported queue kind: {kind!r}")

    def pop_queue(self, kind: QueueKind) -> tuple[Message | None, QueueUpdated]:
        """Remove the latest active queued message of one kind, if any."""

        harness = self._active_queue_harness()
        if kind == "steering":
            message = harness.pop_latest_steering()
        elif kind == "follow_up":
            message = harness.pop_latest_follow_up()
        else:
            raise ValueError(f"Unsupported queue kind: {kind!r}")
        return message, harness.queue_updated_event()

    def clear_queue(self, kind: QueueKind | None = None) -> tuple[QueuedMessages, QueueUpdated]:
        """Clear active queues and return their previous contents plus the new state."""

        harness = self._active_queue_harness()
        if kind is None:
            cleared = harness.clear_queues()
        elif kind == "steering":
            cleared = QueuedMessages(steering=harness.clear_queue(kind))
        elif kind == "follow_up":
            cleared = QueuedMessages(follow_up=harness.clear_queue(kind))
        else:
            raise ValueError(f"Unsupported queue kind: {kind!r}")
        return cleared, harness.queue_updated_event()

    def _active_queue_harness(self) -> AgentHarness:
        harness = self._active_harness
        if harness is None or not self._accepting_queued_messages:
            raise RuntimeError("CodingSession has no active agent run")
        return harness

    async def _prepare_user_message(
        self,
        content: str,
        *,
        context: ToolContext | None = None,
    ) -> Message:
        expanded, evidence = await expand_skill_invocation(
            content,
            catalog=self.skill_catalog,
            context=context or self.tool_context,
        )
        return Message(role="user", content=expanded, skill_invocation=evidence)

    @staticmethod
    def _queue_updated_from_snapshot(queued: _RetainedQueueState | None) -> QueueUpdated:
        if queued is None:
            return QueueUpdated()
        return QueueUpdated(
            steering=tuple(message.user_visible_content for message in queued.messages.steering),
            follow_up=tuple(message.user_visible_content for message in queued.messages.follow_up),
            steering_mode=queued.steering_mode,
            follow_up_mode=queued.follow_up_mode,
        )

    def _apply_configuration(self, configuration: CodingSessionConfiguration) -> None:
        validate_non_negative_integer(
            configuration.context_reserve_tokens, field="context_reserve_tokens"
        )
        self.provider = configuration.provider
        self.model = configuration.model
        self.effort = configuration.effort
        self.models = configuration.models
        self._cost_estimator = CostEstimator(configuration.models)
        self.tool_context = configuration.tool_context
        self.skill_catalog = configuration.skill_catalog
        self.trusted = configuration.trusted
        self.context_reserve_tokens = configuration.context_reserve_tokens
        self.auto_compaction_enabled = configuration.auto_compaction_enabled

    async def run(
        self,
        prompt: str,
        *,
        session: JsonlSession | None = None,
        history: Sequence[Message] = (),
        operation_id: str | None = None,
        tool_context: ToolContext | None = None,
        operation_instructions: str | None = None,
        operation_tool_names: frozenset[str] | None = None,
    ) -> AsyncIterator[WispEvent]:
        async with self._operation_lock:
            self._operation_active = True
            events = self._run(
                prompt,
                session=session,
                history=history,
                operation_id=operation_id,
                tool_context=tool_context,
                operation_instructions=operation_instructions,
                operation_tool_names=operation_tool_names,
            )
            try:
                async for event in events:
                    yield event
            finally:
                try:
                    await events.aclose()
                finally:
                    self._accepting_queued_messages = False
                    if self._active_harness is not None and self._active_session_id is not None:
                        queued = _retained_queue_state(self._active_harness)
                        if queued.messages.count:
                            self._retained_queues[self._active_session_id] = queued
                        else:
                            self._retained_queues.pop(self._active_session_id, None)
                    self._active_harness = None
                    self._active_session_id = None
                    self._active_persistence = None
                    self._operation_active = False

    async def _run(
        self,
        prompt: str,
        *,
        session: JsonlSession | None = None,
        history: Sequence[Message] = (),
        operation_id: str | None = None,
        tool_context: ToolContext | None = None,
        operation_instructions: str | None = None,
        operation_tool_names: frozenset[str] | None = None,
    ) -> AsyncGenerator[WispEvent, None]:
        operation_context = tool_context or self.tool_context
        user_message = await self._prepare_user_message(prompt, context=operation_context)
        session = session or self.sessions.create()
        self._active_session_id = session.session_id
        self._last_session_id = session.session_id
        recover_history = session.session_id in self._history_refresh_session_ids or any(
            pending.entry.session_id == session.session_id for pending in self._pending_entries
        )
        await self._flush_pending_entries()
        snapshot = await anyio.to_thread.run_sync(session.read_run_snapshot)
        if snapshot.entry_count or recover_history:
            history = snapshot.replay.messages
            self._history_refresh_session_ids.discard(session.session_id)
        persistence = _RunPersistence(
            session=session,
            expected_active_leaf_id=snapshot.active_leaf_id,
            operation_id=operation_id,
        )
        self._active_persistence = persistence

        async def emit(event: WispEvent) -> WispEvent:
            return await self._emit(event, session=session, operation_id=operation_id)

        operation_registry = self._operation_tool_registry()
        effective_tools = self._effective_tools(operation_registry)
        operation_policy: ToolPolicy | None = None
        if operation_tool_names is not None:
            effective_tools = tuple(
                tool for tool in effective_tools if tool.name in operation_tool_names
            )
            allowed_names = {tool.name for tool in effective_tools}
            if operation_registry is not None:
                policy_allowed_names: set[str] = set()
                for name in allowed_names:
                    try:
                        operation_tool = operation_registry.get(name)
                    except UnknownToolError:
                        continue
                    if self.tool_policy.allows(operation_tool):
                        policy_allowed_names.add(name)
                allowed_names = policy_allowed_names
                effective_tools = tuple(
                    tool for tool in effective_tools if tool.name in allowed_names
                )
            operation_policy = ToolPolicy.allow_tool_names(allowed_names)
        prompt_messages = self._prompt_messages(
            effective_tools,
            registry=operation_registry,
            context=operation_context,
        )
        if operation_instructions:
            prompt_messages = (
                *prompt_messages,
                Message(role="system", content=operation_instructions),
            )
        executor = ConfiguredToolExecutor(
            registry=operation_registry,
            context=operation_context,
            policy=(
                operation_policy
                if operation_policy is not None
                else self._effective_tool_policy(effective_tools)
            ),
            approval_policy=self.tool_approval_policy,
        )
        harness = AgentHarness(
            AgentHarnessConfig(
                provider=self.provider,
                tool_executor=executor,
                model=self.model,
                tools=effective_tools,
                max_tool_iterations=self.max_tool_iterations,
                effort=self.effort,
                prompt_cache_key=_prompt_cache_key(session.session_id),
                context_window=self._context_window(),
                context_reserve_tokens=self._effective_context_reserve_tokens(),
                cost_estimator=self._cost_estimator,
            ),
            messages=(*prompt_messages, *self._conversation_history(history)),
        )
        retained = self._retained_queues.pop(session.session_id, None)
        if retained is not None:
            harness.set_steering_mode(retained.steering_mode)
            harness.set_follow_up_mode(retained.follow_up_mode)
        retained_messages = retained.messages if retained is not None else QueuedMessages()
        for message in retained_messages.steering:
            harness.steer_message(message)
        for message in retained_messages.follow_up:
            harness.follow_up_message(message)
        self._active_harness = harness
        self._accepting_queued_messages = True
        await self._repair_and_flush(session, harness)
        run_snapshot = await anyio.to_thread.run_sync(session.read_run_snapshot)
        run_entry_start = run_snapshot.entry_count
        run_start_leaf_id = run_snapshot.active_leaf_id

        async def rollback_active_prompt() -> None:
            if operation_id is not None:
                restored = await session.restore_active_leaf_for_operation(
                    run_entry_start,
                    run_start_leaf_id,
                    operation_id=operation_id,
                )
                if restored:
                    persistence.expected_active_leaf_id = run_start_leaf_id
                return
            current_leaf_id = await anyio.to_thread.run_sync(session.read_active_leaf_id)
            if current_leaf_id != run_start_leaf_id:
                await session.select_active_leaf(
                    run_start_leaf_id,
                    expected_active_leaf_id=current_leaf_id,
                    operation_id=operation_id,
                )
                persistence.expected_active_leaf_id = run_start_leaf_id

        yield await emit(AgentStarted(session_id=session.session_id))

        for prompt_message in prompt_messages:
            await persistence.append_message(prompt_message)

        user_entry = await persistence.append_message(user_message)
        # Add the persisted prompt before checking the threshold so provider limits
        # apply to the complete next request, not only the resumed history.
        harness.append_message(user_message)
        if user_message.skill_invocation is not None:
            yield await emit(
                SkillInvoked(
                    session_id=session.session_id,
                    message_entry_id=user_entry.id,
                    invocation=user_message.skill_invocation,
                    provider_content=user_message.content,
                )
            )
        auto_compaction_status = _AutoCompactionStatus()
        # A preflight compaction can only update a request that is actually
        # resuming persisted history. It is reserved for cataloged provider
        # limits; the existing generic reserve policy remains post-response.
        provider_auto_compaction = (
            self.auto_compaction_enabled and self._has_provider_auto_compaction_limit()
        )
        if provider_auto_compaction:
            if history:
                prompt_compacted = False
                async for compaction_event in self._maybe_auto_compact(
                    session,
                    harness,
                    status=auto_compaction_status,
                    operation_id=operation_id,
                    preflight_active_entry_id=user_entry.id,
                ):
                    if (
                        isinstance(compaction_event, CompactionCompleted)
                        and compaction_event.outcome == "completed"
                    ):
                        prompt_compacted = True
                    yield compaction_event
                if prompt_compacted:
                    active_history = await anyio.to_thread.run_sync(session.read_context_messages)
                    harness.replace_messages(
                        (*prompt_messages, *self._conversation_history(active_history))
                    )
            remaining_budget = self._harness_context_budget(harness)
            if self._exceeds_provider_auto_compaction_limit(
                remaining_budget
            ) and self._recover_via_tool_result_truncation(harness, remaining_budget):
                # Recovers a session resumed after a crash mid-turn: the crashed
                # turn's full, untruncated tool result was already durably
                # persisted before any in-memory truncation could run, and an
                # incomplete turn (no closing assistant message) can never be
                # compacted away, so it would otherwise re-trigger this same
                # overflow on every future prompt in the session, forever.
                remaining_budget = self._harness_context_budget(harness)
            if self._exceeds_provider_auto_compaction_limit(remaining_budget):
                error_message = (
                    "Active prompt exceeds the provider auto-compaction limit "
                    "after compacting all eligible history"
                )
                await rollback_active_prompt()
                yield await emit(ErrorEvent(message=error_message))
                yield await emit(
                    AgentCompleted(
                        session_id=session.session_id,
                        turns=0,
                        outcome="failed",
                    )
                )
                raise ContextOverflowError(error_message)

        if provider_auto_compaction:
            # Steering queued while preflight compaction was running must affect
            # the first provider request, not wait for that request to finish.
            self._accepting_queued_messages = False
            try:
                steering_injected = False
                for queue_event in harness.drain_steering():
                    if isinstance(queue_event, QueueMessageInjected):
                        steering_injected = True
                        queue_entry_id = self._queue_message(
                            session,
                            Message(
                                role="user",
                                content=queue_event.content,
                                skill_invocation=queue_event.skill_invocation,
                                created_at=queue_event.timestamp,
                            ),
                            operation_id=operation_id,
                        )
                        if queue_event.skill_invocation is not None:
                            yield await emit(
                                SkillInvoked(
                                    session_id=session.session_id,
                                    message_entry_id=queue_entry_id,
                                    invocation=queue_event.skill_invocation,
                                    provider_content=queue_event.content,
                                    queue_kind=queue_event.kind,
                                )
                            )
                    yield await emit(queue_event)
                if steering_injected:
                    steering_compacted = False
                    async for compaction_event in self._maybe_auto_compact(
                        session,
                        harness,
                        status=auto_compaction_status,
                        operation_id=operation_id,
                        preflight_active_entry_id=user_entry.id,
                    ):
                        if (
                            isinstance(compaction_event, CompactionCompleted)
                            and compaction_event.outcome == "completed"
                        ):
                            steering_compacted = True
                        yield compaction_event
                    if steering_compacted:
                        active_history = await anyio.to_thread.run_sync(
                            session.read_context_messages
                        )
                        harness.replace_messages(
                            (*prompt_messages, *self._conversation_history(active_history))
                        )
                    remaining_budget = self._harness_context_budget(harness)
                    if self._exceeds_provider_auto_compaction_limit(remaining_budget):
                        error_message = (
                            "Active prompt and steering exceed the provider "
                            "auto-compaction limit after compacting all eligible history"
                        )
                        await rollback_active_prompt()
                        yield await emit(ErrorEvent(message=error_message))
                        yield await emit(
                            AgentCompleted(
                                session_id=session.session_id,
                                turns=0,
                                outcome="failed",
                            )
                        )
                        raise ContextOverflowError(error_message)
            finally:
                self._accepting_queued_messages = True

        turns = 0
        had_tool_round = False
        tool_iterations = 0
        had_unsafe_tool_round = False
        overflow_recovery_attempted = False
        recovered_from_overflow = False
        tool_presentation_statuses: dict[str, ToolPresentationStatus] = {}
        harness_events = harness.continue_(
            defer_context_overflow_errors=True,
            pause_after_tool_round=provider_auto_compaction,
        )

        while True:
            saw_loop_error = False
            attempt_had_tool_round = False
            overflow_error: ContextOverflowError | None = None
            overflow_budget: ContextBudget | None = None
            attempt_had_delta = False
            try:
                async for event in harness_events:
                    if isinstance(event, TurnStarted):
                        turns = event.turn
                        attempt_had_delta = False
                    elif isinstance(event, ContextEstimated):
                        overflow_budget = event.budget
                    elif isinstance(event, MessageDelta):
                        # Print and line-oriented UIs cannot retract streamed output.
                        attempt_had_delta = True
                    elif isinstance(event, ErrorEvent):
                        saw_loop_error = True
                    elif isinstance(event, MessageCompleted) and (
                        event.tool_calls or event.finish_reason == "tool_calls"
                    ):
                        had_tool_round = True
                        attempt_had_tool_round = True
                        tool_iterations += 1
                    elif isinstance(event, ToolExecutionEnded) and self._tool_is_unsafe(event.name):
                        had_unsafe_tool_round = True
                    elif isinstance(event, ToolApprovalResolved) and not event.approved:
                        tool_presentation_statuses[event.call_id] = "denied"
                    completion_entry_id: str | None = None
                    if isinstance(event, QueueMessageInjected):
                        queue_entry_id = self._queue_message(
                            session,
                            Message(
                                role="user",
                                content=event.content,
                                skill_invocation=event.skill_invocation,
                                created_at=event.timestamp,
                            ),
                            operation_id=operation_id,
                        )
                        if event.skill_invocation is not None:
                            yield await emit(
                                SkillInvoked(
                                    session_id=session.session_id,
                                    message_entry_id=queue_entry_id,
                                    invocation=event.skill_invocation,
                                    provider_content=event.content,
                                    queue_kind=event.kind,
                                )
                            )
                    if isinstance(event, MessageCompleted | ToolExecutionEnded):
                        tool_status = (
                            tool_presentation_statuses.pop(event.call_id, None)
                            if isinstance(event, ToolExecutionEnded)
                            else None
                        )
                        completion_entry_id = self._queue_completion(
                            session,
                            event,
                            operation_id=operation_id,
                            tool_status=tool_status,
                        )
                    if (
                        isinstance(event, MessageCompleted)
                        and event.finish_reason not in {"error", "cancelled"}
                        and event.usage is not None
                        and event.usage.total_tokens > 0
                        and not event.tool_calls
                        and not had_tool_round
                        and completion_entry_id is not None
                    ):
                        current_messages = self._normalize_provider_messages(harness.messages)
                        self._context_observations[session.session_id] = _ContextObservation(
                            provider=self.provider.name,
                            model=self.model or self.provider.default_model,
                            tokens=event.usage.total_tokens,
                            entry_id=completion_entry_id,
                            context_fingerprint=context_fingerprint(
                                current_messages, effective_tools
                            ),
                        )

                    yield await emit(event)
            except ContextOverflowError as exc:
                overflow_error = exc
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

            if overflow_error is None:
                if attempt_had_tool_round and provider_auto_compaction:
                    tool_round_compacted = False
                    async for compaction_event in self._maybe_auto_compact(
                        session,
                        harness,
                        status=auto_compaction_status,
                        operation_id=operation_id,
                        preflight_active_entry_id=user_entry.id,
                    ):
                        if (
                            isinstance(compaction_event, CompactionCompleted)
                            and compaction_event.outcome == "completed"
                        ):
                            tool_round_compacted = True
                        yield compaction_event
                    if tool_round_compacted:
                        active_history = await anyio.to_thread.run_sync(
                            session.read_context_messages
                        )
                        harness.replace_messages(
                            (*prompt_messages, *self._conversation_history(active_history))
                        )
                    remaining_budget = self._harness_context_budget(harness)
                    if self._exceeds_provider_auto_compaction_limit(
                        remaining_budget
                    ) and self._recover_via_tool_result_truncation(harness, remaining_budget):
                        remaining_budget = self._harness_context_budget(harness)
                    if self._exceeds_provider_auto_compaction_limit(remaining_budget):
                        error_message = (
                            "Active tool result exceeds the provider auto-compaction limit "
                            "after compacting all eligible history"
                        )
                        await rollback_active_prompt()
                        yield await emit(ErrorEvent(message=error_message))
                        yield await emit(
                            AgentCompleted(
                                session_id=session.session_id,
                                turns=turns,
                                outcome="failed",
                            )
                        )
                        raise ContextOverflowError(error_message)
                    harness_events = harness.continue_(
                        turn_offset=turns,
                        tool_iteration_offset=tool_iterations,
                        defer_context_overflow_errors=True,
                        pause_after_tool_round=True,
                    )
                    continue
                break

            can_retry_overflow = (
                not overflow_recovery_attempted
                and self.auto_compaction_enabled
                and not attempt_had_delta
                and not had_unsafe_tool_round
                and overflow_budget is not None
                and (
                    overflow_budget.context_window is None
                    or overflow_budget.reserve_tokens < overflow_budget.context_window
                )
            )
            if not can_retry_overflow:
                yield await emit(ErrorEvent(message=str(overflow_error)))
                yield await emit(TurnCompleted(turn=turns, outcome="failed", finish_reason="error"))
                yield await emit(
                    AgentCompleted(session_id=session.session_id, turns=turns, outcome="failed")
                )
                raise overflow_error from None

            replay: SessionReplay | None = None
            try:
                replay = await self._prepare_compaction_replay(session)
                plan = plan_manual_compaction(replay)
            except NothingToCompactError:
                yield await emit(ErrorEvent(message=str(overflow_error)))
                yield await emit(TurnCompleted(turn=turns, outcome="failed", finish_reason="error"))
                yield await emit(
                    AgentCompleted(session_id=session.session_id, turns=turns, outcome="failed")
                )
                raise overflow_error from None
            except Exception as exc:
                source_entry_count = len(replay.context_entry_ids) if replay is not None else 0
                yield await self._emit_recoverable_event(
                    CompactionStarted(
                        session_id=session.session_id,
                        reason="overflow",
                        source_entry_count=source_entry_count,
                        trigger_budget=overflow_budget,
                    ),
                    session=session,
                    operation_id=operation_id,
                )
                yield await self._emit_recoverable_event(
                    CompactionCompleted(
                        session_id=session.session_id,
                        reason="overflow",
                        outcome="failed",
                        replaced_entry_count=0,
                        retained_entry_count=source_entry_count,
                        provider=self.provider.name,
                        model=self.model or self.provider.default_model,
                        error=str(exc),
                    ),
                    session=session,
                    operation_id=operation_id,
                )
                recovery_error = f"Context overflow recovery failed: {exc}"
                yield await emit(ErrorEvent(message=recovery_error))
                yield await emit(TurnCompleted(turn=turns, outcome="failed", finish_reason="error"))
                yield await emit(
                    AgentCompleted(session_id=session.session_id, turns=turns, outcome="failed")
                )
                raise ContextOverflowError(recovery_error) from overflow_error

            overflow_compaction: CompactionCompleted | None = None

            async def prepare_retry() -> None:
                active_history = await anyio.to_thread.run_sync(session.read_context_messages)
                harness.replace_messages(
                    (*prompt_messages, *self._conversation_history(active_history))
                )

            async for compaction_event in self._compact_locked(
                session,
                plan,
                reason="overflow",
                instructions=None,
                trigger_budget=overflow_budget,
                recover_failure=True,
                will_retry=True,
                operation_id=operation_id,
                retry_setup=prepare_retry,
            ):
                if isinstance(compaction_event, CompactionCompleted):
                    overflow_compaction = compaction_event
                yield compaction_event

            if (
                overflow_compaction is None
                or overflow_compaction.outcome != "completed"
                or not overflow_compaction.will_retry
            ):
                detail = (
                    overflow_compaction.error
                    if overflow_compaction is not None and overflow_compaction.error
                    else "compaction did not complete"
                )
                recovery_error = f"Context overflow recovery failed: {detail}"
                yield await emit(ErrorEvent(message=recovery_error))
                yield await emit(TurnCompleted(turn=turns, outcome="failed", finish_reason="error"))
                yield await emit(
                    AgentCompleted(session_id=session.session_id, turns=turns, outcome="failed")
                )
                raise ContextOverflowError(recovery_error) from overflow_error

            overflow_recovery_attempted = True
            recovered_from_overflow = True
            yield await emit(TurnCompleted(turn=turns, outcome="failed", finish_reason="error"))
            harness_events = harness.continue_(
                turn_offset=turns,
                tool_iteration_offset=tool_iterations,
                defer_context_overflow_errors=True,
                pause_after_tool_round=provider_auto_compaction,
            )

        self._accepting_queued_messages = False
        auto_compaction_saved = False
        if not recovered_from_overflow:
            async for compaction_event in self._maybe_auto_compact(
                session,
                harness,
                status=auto_compaction_status,
                operation_id=operation_id,
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
            self._operation_active = True
            self._active_persistence = _RunPersistence(
                session=session,
                expected_active_leaf_id=session.read_active_leaf_id(),
                operation_id=None,
            )
            try:
                replay = await self._prepare_compaction_replay(session)
                self._active_persistence = _RunPersistence(
                    session=session,
                    expected_active_leaf_id=replay.active_leaf_id,
                    operation_id=None,
                )
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
            finally:
                self._active_persistence = None
                self._operation_active = False

    @staticmethod
    def _exceeds_provider_auto_compaction_limit(budget: ContextBudget) -> bool:
        return (
            budget.context_window is not None and budget.reserve_tokens >= budget.context_window
        ) or should_auto_compact(budget, enabled=True)

    @staticmethod
    def _provider_auto_compaction_excess_tokens(budget: ContextBudget) -> int | None:
        """Return how far over the provider's auto-compaction limit ``budget`` is.

        Returns ``None`` when the overage cannot be a truncatable token excess — a
        ``reserve_tokens >= context_window`` configuration is unfixable by shrinking
        message content, so the caller must not attempt truncation recovery for it.
        """

        if budget.context_window is None or budget.reserve_tokens >= budget.context_window:
            return None
        tokens = (
            budget.observed_tokens
            if budget.observed_is_current and budget.observed_tokens is not None
            else budget.estimate.total_tokens
        )
        excess = tokens - (budget.context_window - budget.reserve_tokens)
        return excess if excess > 0 else None

    def _recover_via_tool_result_truncation(
        self, harness: AgentHarness, budget: ContextBudget
    ) -> bool:
        """Shrink the active turn's tool results to fit under the provider limit.

        Called after a compaction attempt still leaves the budget exceeded —
        whether because compaction had nothing left to summarize, or because it
        fully compacted history and the active turn's own tool results are what's
        left over budget. Compaction can never touch the active turn (it is always
        retained), so shrinking its tool results is the only remaining lever before
        the terminal overflow error. Returns whether truncation changed the
        transcript; the caller re-checks the budget afterward regardless.
        """

        excess_tokens = self._provider_auto_compaction_excess_tokens(budget)
        if excess_tokens is None:
            return False
        # Reclaim a margin beyond the bare excess so one truncation pass is enough
        # in the common case, rather than converging token-by-token across retries.
        truncated = truncate_active_turn_tool_results(
            harness.messages, excess_tokens=excess_tokens + excess_tokens // 4 + 64
        )
        if truncated is None:
            return False
        harness.replace_messages(truncated)
        return True

    def _harness_context_budget(self, harness: AgentHarness) -> ContextBudget:
        return build_context_budget(
            estimate_context(
                self._normalize_provider_messages(harness.messages),
                tuple(harness.config.tools),
            ),
            context_window=self._context_window(),
            reserve_tokens=self._effective_context_reserve_tokens(),
        )

    async def _maybe_auto_compact(
        self,
        session: JsonlSession,
        harness: AgentHarness,
        *,
        status: _AutoCompactionStatus,
        operation_id: str | None = None,
        preflight_active_entry_id: str | None = None,
    ) -> AsyncIterator[WispEvent]:
        provider_messages = self._normalize_provider_messages(harness.messages)
        tools = tuple(harness.config.tools)
        estimate = estimate_context(provider_messages, tools)
        observation = self._context_observations.get(session.session_id)
        current_fingerprint = context_fingerprint(provider_messages, tools)
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
            reserve_tokens=self._effective_context_reserve_tokens(),
            observed_tokens=observed_tokens,
            observed_is_current=observed_is_current,
        )
        if not should_auto_compact(budget, enabled=self.auto_compaction_enabled):
            return

        replay: SessionReplay | None = None
        try:
            replay = await self._prepare_compaction_replay(session)
            plan = (
                plan_preflight_compaction(
                    replay,
                    active_turn_entry_id=preflight_active_entry_id,
                )
                if preflight_active_entry_id is not None
                else plan_manual_compaction(replay)
            )
        except NothingToCompactError:
            # Over budget, but every row is already part of the active turn or a
            # prior summary — there is nothing left this call can summarize away.
            # The caller falls back to truncating the active turn's own tool
            # results (the only lever left) once it sees the budget still exceeded.
            return
        except Exception as exc:
            status.skip_final_save = any(
                pending.entry.session_id == session.session_id for pending in self._pending_entries
            )
            source_entry_count = len(replay.context_entry_ids) if replay is not None else 0
            yield await self._emit_recoverable_event(
                CompactionStarted(
                    session_id=session.session_id,
                    reason="threshold",
                    source_entry_count=source_entry_count,
                    trigger_budget=budget,
                ),
                session=session,
                operation_id=operation_id,
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
                operation_id=operation_id,
            )
            return
        async for event in self._compact_locked(
            session,
            plan,
            reason="threshold",
            instructions=None,
            trigger_budget=budget,
            recover_failure=True,
            operation_id=operation_id,
        ):
            yield event

    async def _prepare_compaction_replay(self, session: JsonlSession) -> SessionReplay:
        await self._flush_pending_entries()
        replay = await self._read_context(session)
        repair_plan = plan_interrupted_tool_repairs(replay.messages)
        if repair_plan.repairs:
            for repair in repair_plan.repairs:
                self._queue_message(
                    session,
                    repair,
                    tool_result=ToolResultPresentationSnapshot(status="cancelled"),
                )
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
        will_retry: bool = False,
        operation_id: str | None = None,
        retry_setup: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[WispEvent]:
        started = CompactionStarted(
            session_id=session.session_id,
            reason=reason,
            source_entry_count=len(plan.expected_context_entry_ids),
            trigger_budget=trigger_budget,
        )
        provider_name = self.provider.name
        effective_model = self.model or self.provider.default_model
        summary_committed = False
        summary: CompactionSummary | None = None
        try:
            if recover_failure:
                yield await self._emit_recoverable_event(
                    started,
                    session=session,
                    operation_id=operation_id,
                )
            else:
                yield await self._emit(started, session=session, operation_id=operation_id)
            summary = await summarize_manual_compaction(
                plan,
                provider=self.provider,
                model=self.model,
                effort=self.effort,
                prompt_cache_key=_prompt_cache_key(session.session_id),
                instructions=instructions,
                cost_estimator=self._cost_estimator,
                context_window=self._context_window(),
                reserve_tokens=self.context_reserve_tokens,
            )
            normalized_instructions = (
                instructions.strip() if instructions is not None and instructions.strip() else None
            )
            entry = CompactionSessionEntry(
                session_id=session.session_id,
                compaction=CompactionRecord(
                    schema_version=4,
                    summary=summary.summary,
                    replaced_entry_ids=plan.replaced_entry_ids,
                    provider=provider_name,
                    model=effective_model,
                    instructions=normalized_instructions,
                    usage=summary.usage,
                    cost=summary.cost,
                    reason=reason,
                    trigger_budget=trigger_budget,
                ),
                operation_id=operation_id,
            )
            publication_errors: list[str] = []
            with anyio.CancelScope(shield=True):
                persistence = self._active_persistence
                if persistence is None or persistence.session.session_id != session.session_id:
                    raise RuntimeError("Compaction has no active session persistence cursor")
                try:
                    await persistence.append_compaction(
                        entry,
                        expected_context_entry_ids=plan.expected_context_entry_ids,
                    )
                except Exception:
                    # A write can commit and then fail during post-append validation.
                    # Retrying the stable entry id reconciles that uncertain outcome.
                    await persistence.append_compaction(
                        entry,
                        expected_context_entry_ids=plan.expected_context_entry_ids,
                    )
                summary_committed = True
                self._history_refresh_session_ids.add(session.session_id)
                self._context_observations.pop(session.session_id, None)
                saved = SessionSaved(session_id=session.session_id, path=session.path)
                try:
                    await self._emit(saved, session=session, operation_id=operation_id)
                except Exception as exc:
                    publication_errors.append(str(exc))

            retry_setup_error: str | None = None
            if retry_setup is not None:
                try:
                    await retry_setup()
                except Exception as exc:
                    retry_setup_error = str(exc) or type(exc).__name__
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
                cost=summary.cost,
                will_retry=will_retry and retry_setup_error is None,
                error=(
                    retry_setup_error
                    or (
                        "Event publication failed: " + "; ".join(publication_errors)
                        if publication_errors and recover_failure
                        else None
                    )
                ),
            )
            with anyio.CancelScope(shield=True):
                try:
                    await self._emit(completed, session=session, operation_id=operation_id)
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
        except StaleSessionWriterError:
            raise
        except Exception as exc:
            error = str(exc)
            summary_usage = exc.usage if isinstance(exc, CompactionSummaryError) else None
            summary_cost = exc.cost if isinstance(exc, CompactionSummaryError) else None
            if summary is not None:
                summary_usage = summary_usage if summary_usage is not None else summary.usage
                summary_cost = summary_cost if summary_cost is not None else summary.cost
            if not recover_failure:
                yield await self._emit(
                    ErrorEvent(message=error),
                    session=session,
                    operation_id=operation_id,
                )
            failed = CompactionCompleted(
                session_id=session.session_id,
                reason=reason,
                outcome="failed",
                replaced_entry_count=len(plan.replaced_entry_ids),
                retained_entry_count=len(plan.retained_rows),
                provider=provider_name,
                model=effective_model,
                usage=summary_usage,
                cost=summary_cost,
                error=error,
            )
            if not summary_committed and (summary_usage is not None or summary_cost is not None):
                try:
                    persistence = self._active_persistence
                    if (
                        persistence is not None
                        and persistence.session.session_id == session.session_id
                    ):
                        await persistence.append_event(failed)
                    else:
                        await session.append_event(failed, operation_id=operation_id)
                except StaleSessionWriterError:
                    raise
                except Exception:
                    if not recover_failure:
                        raise
            if recover_failure:
                yield await self._emit_recoverable_event(
                    failed,
                    session=session,
                    operation_id=operation_id,
                )
            else:
                yield await self._emit(failed, session=session, operation_id=operation_id)
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
                    cancelled = await self._emit_recoverable_event(
                        cancelled_event,
                        session=session,
                        operation_id=operation_id,
                    )
                else:
                    cancelled = await self._emit(
                        cancelled_event,
                        session=session,
                        operation_id=operation_id,
                    )
            yield cancelled
            raise

    async def _emit_recoverable_event(
        self,
        event: WispEvent,
        *,
        session: JsonlSession,
        operation_id: str | None = None,
    ) -> WispEvent:
        try:
            return await self._emit(
                event,
                session=session,
                operation_id=operation_id,
            )
        except Exception:
            return event

    async def get_session_stats(self, session: JsonlSession | None = None) -> SessionStats:
        """Return a consistent, non-persisted statistics snapshot."""

        async with self._operation_lock:
            await self._flush_pending_entries()
            if session is None or not session.path.exists():
                entries: tuple[SessionEntry, ...] = ()
            else:
                entries = await anyio.to_thread.run_sync(
                    session.read_entries,
                    abandon_on_cancel=True,
                )
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
                tools=self._effective_tools(),
                context_window=self._context_window(),
                reserve_tokens=self._effective_context_reserve_tokens(),
                observed_tokens=observed_tokens,
                observed_is_current=observed_is_current,
                observed_entry_id=observed_entry_id,
                observed_context_fingerprint=observed_context_fingerprint,
                auto_compaction_enabled=self.auto_compaction_enabled,
            )

    def _allowed_tool_specs(self, tool_registry: ToolRegistry) -> tuple[ToolSpec, ...]:
        return tuple(
            ToolSpec.from_tool(tool)
            for tool in tool_registry.all()
            if self.tool_policy.allows(tool)
        )

    def _tool_is_unsafe(self, name: str) -> bool:
        """Return whether a replayed tool turn could repeat side effects on recovery."""

        if self.tool_registry is None:
            return True
        try:
            return self.tool_registry.get(name).safety != "read"
        except UnknownToolError:
            return True

    def _effective_tools(self, registry: ToolRegistry | None = None) -> tuple[ToolSpec, ...]:
        """Return the startup-authorized tools available in the current mode."""

        effective_registry = registry if registry is not None else self.tool_registry
        if self.mode == "build" or effective_registry is None:
            return self.tools if self.mode == "build" else ()
        build_names = {tool.name for tool in self.tools}
        return tuple(
            ToolSpec.from_tool(tool)
            for tool in effective_registry.all()
            if tool.name in build_names and tool.safety == "read" and self.tool_policy.allows(tool)
        )

    def _effective_tool_policy(self, tools: Sequence[ToolSpec]) -> ToolPolicy:
        if self.mode == "build":
            return self.tool_policy
        return ToolPolicy.allow_tool_names({tool.name for tool in tools})

    def _prompt_messages(
        self,
        tools: Sequence[ToolSpec] | None = None,
        *,
        registry: ToolRegistry | None = None,
        context: ToolContext | None = None,
    ) -> tuple[Message, ...]:
        effective_tools = tuple(tools) if tools is not None else self._effective_tools()
        operation_context = context or self.tool_context
        effective_registry = registry if registry is not None else self.tool_registry
        effective_names = {tool.name for tool in effective_tools}
        skill_index = (
            build_skill_index(self.skill_catalog)
            if effective_registry is not None
            and any(
                type(tool) is SkillTool and tool.name in effective_names
                for tool in effective_registry.all()
            )
            else ""
        )
        if self.prompt_messages is not None:
            messages = (
                (*self.prompt_messages, Message(role="system", content=skill_index))
                if skill_index
                else self.prompt_messages
            )
            if self.mode == "plan":
                return (*messages, Message(role="system", content=PLAN_MODE_SYSTEM_PROMPT))
            return messages
        return build_prompt_messages(
            cwd=operation_context.cwd,
            tools=effective_tools,
            tool_prompt_metadata=effective_registry.prompt_metadata(
                tool.name for tool in effective_tools
            )
            if effective_registry is not None
            else (),
            additional_guidance=(skill_index,) if skill_index else (),
            mode=self.mode,
            max_context_chars=self.project_context_max_chars,
            include_project_context=self.trusted,
            protected_paths=operation_context.protected_paths,
            trusted_context_root=self.project_context_root
            or resolve_project_context_root(operation_context.cwd),
        )

    def _operation_tool_registry(self) -> ToolRegistry | None:
        """Bind the selected skill tool to this operation's immutable catalog snapshot."""

        if self.tool_registry is None:
            return None
        registry = ToolRegistry()
        for tool in self.tool_registry.all():
            operation_tool = SkillTool(self.skill_catalog) if type(tool) is SkillTool else tool
            registry.register(
                operation_tool,
                prompt=self.tool_registry.prompt_metadata_for(tool.name),
            )
        return registry

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

    def _effective_context_reserve_tokens(self) -> int:
        if self.models is None:
            return self.context_reserve_tokens
        return self.models.effective_context_reserve_tokens(
            self.provider.name,
            self.model,
            reserve_tokens=self.context_reserve_tokens,
            default_model=self.provider.default_model,
        )

    def _has_provider_auto_compaction_limit(self) -> bool:
        if self.models is None:
            return False
        return (
            self.models.auto_compact_token_limit(
                self.provider.name,
                self.model,
                default_model=self.provider.default_model,
            )
            is not None
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
            persistence = self._active_persistence
            if persistence is not None and persistence.session.session_id == session.session_id:
                await persistence.append_event(event)
            else:
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
        tool_status: ToolPresentationStatus | None = None,
    ) -> str:
        tool_result = (
            ToolResultPresentationSnapshot(
                status=tool_status or _tool_result_status(event),
                exit_code=event.exit_code,
                output_has_exit_status=event.output_has_exit_status,
                before_text=event.before_text,
                created=event.created,
                summary=event.summary,
                truncated=event.truncated,
            )
            if isinstance(event, ToolExecutionEnded)
            else None
        )
        return self._queue_message(
            session,
            message_from_completion_event(event),
            operation_id=operation_id,
            tool_result=tool_result,
        )

    def _queue_message(
        self,
        session: JsonlSession,
        message: Message,
        *,
        operation_id: str | None = None,
        tool_result: ToolResultPresentationSnapshot | None = None,
    ) -> str:
        entry = MessageSessionEntry(
            session_id=session.session_id,
            message=message,
            operation_id=operation_id,
            created_at=message.created_at,
            tool_result=tool_result,
        )
        persistence = self._active_persistence
        if persistence is None or persistence.session.session_id != session.session_id:
            persistence = _RunPersistence(
                session=session,
                expected_active_leaf_id=session.read_active_leaf_id(),
                operation_id=operation_id,
            )
        self._pending_entries.append(_PendingSessionEntry(persistence=persistence, entry=entry))
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
            self._queue_message(
                session,
                message,
                operation_id=operation_id,
                tool_result=ToolResultPresentationSnapshot(status="cancelled"),
            )
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
                    await pending.persistence.append_entry(pending.entry)
                except StaleSessionWriterError:
                    self._history_refresh_session_ids.add(pending.entry.session_id)
                    self._pending_entries = deque(
                        queued
                        for queued in self._pending_entries
                        if queued.persistence is not pending.persistence
                    )
                    raise
                except BaseException:
                    self._history_refresh_session_ids.add(pending.entry.session_id)
                    raise
                self._pending_entries.popleft()


def _prompt_cache_key(session_id: str) -> str:
    """Return the stable prompt-cache namespace for one durable session."""

    return f"wisp:{session_id}"


def _tool_result_status(event: ToolExecutionEnded) -> ToolPresentationStatus:
    return tool_result_status(
        event.is_error,
        event.exit_code,
        process_state=event.process_state,
    )


__all__ = ["CodingSession", "PERSISTED_SESSION_EVENT_TYPES"]
