"""Application-facing coding session built on the portable agent harness."""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio

from wisp.agent.context_budget import (
    build_context_budget,
    estimate_context,
    estimate_context_budget,
)
from wisp.agent.harness import (
    AgentHarness,
    AgentHarnessConfig,
    HarnessBoundaryContext,
    QueuedMessages,
)
from wisp.agent.request_boundary import (
    ContextOverflowFailure,
    ContextOverflowSnapshot,
    RequestBoundaryDecision,
    RequestContextRebase,
)
from wisp.agent.transcript_repair import plan_interrupted_tool_repairs
from wisp.agent.turn_lifecycle import TurnLifecycle
from wisp.agent.validation import (
    validate_non_negative_integer,
    validate_optional_non_negative_integer,
)
from wisp.coding.compaction import (
    CompactionSummary,
    CompactionSummaryError,
    ManualCompactionPlan,
    NothingToCompactError,
    exceeds_provider_auto_compaction_limit,
    matches_provider_suffix,
    plan_manual_compaction,
    plan_preflight_compaction,
    provider_auto_compaction_excess_tokens,
    should_auto_compact,
    summarize_manual_compaction,
    truncate_active_turn_tool_results,
)
from wisp.coding.configuration import CodingSessionConfiguration
from wisp.coding.costs import CostEstimator
from wisp.coding.persistence import PendingSessionEntry, RunPersistence
from wisp.coding.prompt import (
    DEFAULT_CONTEXT_MAX_CHARS,
    PROJECT_CONTEXT_HEADER,
    WEB_SEARCH_GUIDANCE,
    WEB_SEARCH_UNAVAILABLE_GUIDANCE,
    build_prompt_messages,
    extend_custom_prompt_messages,
    read_repository_status,
)
from wisp.coding.stats import build_session_stats
from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import (
    DEFAULT_AGENT_MODE,
    AgentCompleted,
    AgentMode,
    AgentStarted,
    CodingSessionState,
    CompactionCompleted,
    CompactionReason,
    CompactionStarted,
    ContextBudget,
    ContextObservation,
    ContextOverflow,
    ErrorEvent,
    MessageCompleted,
    QueueKind,
    QueueMessageInjected,
    QueueMode,
    QueueUpdated,
    RunOutcome,
    SessionSaved,
    SessionStats,
    SkillInvoked,
    ToolApprovalResolved,
    ToolExecutionEnded,
    ToolExecutionStarted,
    ToolPresentationStatus,
    TurnCompleted,
    TurnStarted,
    WispEvent,
    utc_now,
)
from wisp.messages import (
    CompactionRecord,
    Message,
    completion_event_has_history,
    message_from_completion_event,
)
from wisp.messages.history import active_turn_start
from wisp.providers.base import (
    ContextOverflowError,
    Provider,
    ToolSpec,
    prepare_provider_history,
    provider_native_output,
    provider_supports_web_search,
)
from wisp.providers.catalog import ModelRegistry
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ToolRegistry, UnknownToolError
from wisp.sessions.entries import (
    CompactionSessionEntry,
    MessageSessionEntry,
    RepositoryStatusSnapshot,
    SessionEntry,
    ToolResultPresentationSnapshot,
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

# A resumed session reuses its persisted Git snapshot only while the provider may
# still hold the cached prefix built from it. After a longer pause the cache has
# expired anyway, so a fresh read costs nothing and avoids a long-stale status.
RESUMED_REPOSITORY_STATUS_MAX_AGE = timedelta(hours=1)

PERSISTED_SESSION_EVENT_TYPES = frozenset(
    {
        "tool.execution.started",
        "tool.call",
        "tool.approval.requested",
        "tool.approval.resolved",
        "tool.execution.ended",
        "context.pressure",
        "context.overflow",
        "web.search",
        "error",
    }
)


@dataclass(frozen=True, slots=True)
class _SessionPrompt:
    """System messages for one run, and the Git snapshot their project context used.

    The snapshot is saved with the project-context message so a resumed session
    can send the same prompt again (see `_persisted_repository_status`).
    """

    messages: tuple[Message, ...]
    repository_status: RepositoryStatusSnapshot | None = None


def _is_project_context(message: Message) -> bool:
    return message.role == "system" and message.content.startswith(f"{PROJECT_CONTEXT_HEADER}\n")


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
        web_search: bool = False,
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
        self._pending_entries: deque[PendingSessionEntry] = deque()
        self._pending_flush_lock = anyio.Lock()
        self._operation_lock = anyio.Semaphore(1)
        self._history_refresh_session_ids: set[str] = set()
        self._context_observations: dict[str, ContextObservation] = {}
        # One Git status per session and working directory: a status that changed on
        # every run would change the prompt prefix and miss the provider's cache for
        # the whole conversation after it.
        self._repository_status_snapshots: dict[tuple[str, Path], str] = {}
        self._operation_active = False
        self._active_harness: AgentHarness | None = None
        self._active_session_id: str | None = None
        self._active_persistence: RunPersistence | None = None
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
                web_search=web_search,
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
            web_search=configuration.web_search,
        )

    @property
    def busy(self) -> bool:
        """Whether a prompt or compaction operation is still running."""

        return self._operation_active

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
            web_search=self.web_search,
        )

    def reconfigure(self, configuration: CodingSessionConfiguration) -> None:
        """Apply a complete configuration between prompt or compaction operations."""

        if self._operation_active:
            raise RuntimeError("CodingSession is busy")
        self._apply_configuration(configuration)

    def replace_tool_registry(self, tool_registry: ToolRegistry) -> None:
        """Use ``tool_registry`` for subsequent agent operations.

        The allowed tool specs are recomputed from it under the current tool policy,
        replacing any tools given at construction.

        Args:
            tool_registry (ToolRegistry): The tools later operations may use.

        Raises:
            RuntimeError: If an operation is active; its tools cannot change mid-run.
        """

        if self._operation_active:
            raise RuntimeError("CodingSession is busy")
        self.tool_registry = tool_registry
        self.tools = self._allowed_tool_specs(tool_registry)

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
            web_search=self.web_search,
            steering_mode=queue.steering_mode,
            follow_up_mode=queue.follow_up_mode,
            pending_steering_count=len(queue.steering),
            pending_follow_up_count=len(queue.follow_up),
        )

    def validate_queue_token(self, expected_token: str) -> None:
        """Reject a stale queue snapshot before a synchronous mutation.

        Args:
            expected_token (str): Opaque token from the displayed queue snapshot.

        Raises:
            RuntimeError: No run accepts queue mutations or the snapshot is stale.
        """
        state = self._active_queue_harness().queue_updated_event()
        if state.token != expected_token:
            raise RuntimeError("Queue changed; refresh and retry the operation")

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
        self.web_search = configuration.web_search

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
        operation_ready: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[WispEvent]:
        """Run one prompt to completion and stream its events.

        Runs are serialized: the session's operation lock is held until the stream
        ends, so a second call waits for the first. Steering and follow-up
        messages are accepted while the run is active. When the stream ends for
        any reason (completion, failure, cancellation, or the caller closing it),
        the run's generator is closed first, then any messages still queued are
        retained for the next run on the same session, and the active run state
        is cleared.

        Args:
            prompt (str): User prompt for this run.
            session (JsonlSession | None, optional): Session to append to. A new
                session is created when omitted. Defaults to None.
            history (Sequence[Message], optional): History to resume when the
                session has no persisted entries. Persisted history takes
                precedence. Defaults to ().
            operation_id (str | None, optional): Operation owning the run's
                entries; also enables operation-scoped rollback. Defaults to None.
            tool_context (ToolContext | None, optional): Tool context for this run
                instead of the session's. Defaults to None.
            operation_instructions (str | None, optional): Extra system
                instructions appended after the prompt messages. Defaults to None.
            operation_tool_names (frozenset[str] | None, optional): Restrict the
                run to these tools, still subject to the session tool policy.
                Defaults to None.
            operation_ready (Callable[[], Awaitable[None]] | None, optional):
                Awaited once the run accepts queued messages, before prompt
                construction. Defaults to None.

        Yields:
            WispEvent: The run's events in publication order. A `QueueUpdated`
            for restored queues may precede `AgentStarted`; a run that reaches
            its agent loop ends with `AgentCompleted`.

        Raises:
            ContextOverflowError: The prompt cannot fit the provider's context, or
                overflow recovery failed; raised after `AgentCompleted(failed)`.
            Exception: A failure inside the agent loop propagates after the open
                turn is completed as failed and `AgentCompleted(failed)` is
                published. A failure while preparing the run propagates without
                a terminal event.
        """

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
                operation_ready=operation_ready,
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
        operation_ready: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncGenerator[WispEvent, None]:
        operation_context = tool_context or self.tool_context
        user_message = await self._prepare_user_message(prompt, context=operation_context)
        session = session or self.sessions.create()
        assert session is not None
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
        persistence = RunPersistence(
            session=session,
            expected_active_leaf_id=snapshot.active_leaf_id,
            operation_id=operation_id,
        )
        self._active_persistence = persistence

        async def emit(event: WispEvent) -> WispEvent:
            return await self._emit(event, session=session, operation_id=operation_id)

        operation_registry = self._operation_tool_registry()
        effective_tools, executor = self._run_tools(
            operation_registry,
            operation_tool_names,
            context=operation_context,
        )
        harness, restored_queues = self._start_run_harness(
            session,
            history,
            tools=effective_tools,
            executor=executor,
        )
        # Publish the operation-owned harness before project-context I/O so
        # steering, follow-ups, queue inspection, and cancellation remain
        # responsive while prompt construction runs in its worker thread.
        self._active_harness = harness
        self._accepting_queued_messages = True
        if operation_ready is not None:
            await operation_ready()
        if restored_queues:
            # The replacement owner gives retained items a new guard token.
            # Publish it before prompt preparation so the first queue mutation
            # need not fail against the previous run's displayed snapshot.
            yield await emit(harness.queue_updated_event())
        session_prompt = await self._prompt_messages_async(
            effective_tools,
            registry=operation_registry,
            context=operation_context,
            session_id=session.session_id,
            session=session,
        )
        prompt_messages = session_prompt.messages
        if operation_instructions:
            prompt_messages = (
                *prompt_messages,
                Message(role="system", content=operation_instructions),
            )
        harness.replace_messages((*prompt_messages, *self._conversation_history(history)))
        await self._repair_and_flush(session, harness)
        run_snapshot = await anyio.to_thread.run_sync(session.read_run_snapshot)
        run_entry_start = run_snapshot.entry_count
        run_start_leaf_id = run_snapshot.active_leaf_id

        # Allocate the prompt's identity before AgentStarted, preserving the
        # existing event order while allowing frontends to identify their echo.
        user_entry = MessageSessionEntry(
            session_id=session.session_id,
            message=user_message,
            operation_id=operation_id,
            created_at=user_message.created_at,
        )
        yield await emit(
            AgentStarted(session_id=session.session_id, message_entry_id=user_entry.id)
        )

        for prompt_message in prompt_messages:
            # The project-context entry also carries its Git snapshot, which
            # `append_message` has no way to attach, so it is built here.
            if session_prompt.repository_status is not None and _is_project_context(prompt_message):
                await persistence.append_entry(
                    MessageSessionEntry(
                        session_id=session.session_id,
                        message=prompt_message,
                        operation_id=operation_id,
                        repository_status=session_prompt.repository_status,
                    )
                )
            else:
                await persistence.append_message(prompt_message)

        await persistence.append_entry(user_entry)
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
        compaction = _RunCompaction(
            self,
            session=session,
            harness=harness,
            persistence=persistence,
            operation_id=operation_id,
            prompt_messages=prompt_messages,
            run_entry_start=run_entry_start,
            run_start_leaf_id=run_start_leaf_id,
            provider_auto_compaction=provider_auto_compaction,
            status=auto_compaction_status,
            prompt_entry_id=user_entry.id,
        )
        if provider_auto_compaction:
            if history:
                async for compaction_event in compaction.compact_before_first_request():
                    yield compaction_event
            remaining_budget = self._harness_context_budget(harness)
            if exceeds_provider_auto_compaction_limit(
                remaining_budget
            ) and self._recover_via_tool_result_truncation(harness, remaining_budget):
                # Recovers a session resumed after a crash mid-turn: the crashed
                # turn's full, untruncated tool result was already durably
                # persisted before any in-memory truncation could run, and an
                # incomplete turn (no closing assistant message) can never be
                # compacted away, so it would otherwise re-trigger this same
                # overflow on every future prompt in the session, forever.
                remaining_budget = self._harness_context_budget(harness)
            if exceeds_provider_auto_compaction_limit(remaining_budget):
                error_message = (
                    "Active prompt exceeds the provider auto-compaction limit "
                    "after compacting all eligible history"
                )
                await compaction.rollback_active_prompt()
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
                        queue_entry_id = self._persist_queue_injection(
                            session, queue_event, operation_id=operation_id
                        )
                        queue_event = queue_event.model_copy(
                            update={"message_entry_id": queue_entry_id}
                        )
                        skill_event = _queued_skill_invoked(
                            session.session_id, queue_entry_id, queue_event
                        )
                        if skill_event is not None:
                            yield await emit(skill_event)
                    yield await emit(queue_event)
                if steering_injected:
                    async for compaction_event in compaction.compact_before_first_request():
                        yield compaction_event
                    remaining_budget = self._harness_context_budget(harness)
                    if exceeds_provider_auto_compaction_limit(remaining_budget):
                        error_message = (
                            "Active prompt and steering exceed the provider "
                            "auto-compaction limit after compacting all eligible history"
                        )
                        await compaction.rollback_active_prompt()
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

        turn_lifecycle = TurnLifecycle()
        tool_presentation_statuses: dict[str, ToolPresentationStatus] = {}
        started_tool_calls: set[str] = set()
        queue_batch_started_new_turn = False
        harness_events = harness.continue_(
            boundary_preparer=compaction,
            context_overflow_hook=compaction,
        )

        terminal_outcome: RunOutcome = "completed"
        while True:
            saw_loop_error = False
            overflow_error: ContextOverflowError | None = None
            try:
                async for event in harness_events:
                    for compaction_event in compaction.take_events():
                        yield compaction_event
                    turn_lifecycle.observe(event)
                    if isinstance(event, TurnStarted):
                        if compaction.recovered_from_overflow:
                            overflow_error = None
                    elif isinstance(event, ErrorEvent):
                        saw_loop_error = True
                    elif isinstance(event, ContextOverflow):
                        overflow_error = ContextOverflowError(event.message)
                    elif isinstance(event, TurnCompleted):
                        terminal_outcome = event.outcome
                        queue_batch_started_new_turn = False
                    elif isinstance(event, ToolExecutionStarted):
                        started_tool_calls.add(event.call_id)
                    elif isinstance(event, ToolExecutionEnded):
                        was_started = event.call_id in started_tool_calls
                        started_tool_calls.discard(event.call_id)
                        if was_started and self._tool_is_unsafe(event.name):
                            compaction.unsafe_tool_ran()
                    elif isinstance(event, ToolApprovalResolved) and not event.approved:
                        tool_presentation_statuses[event.call_id] = "denied"
                    completion_entry_id: str | None = None
                    if isinstance(event, QueueMessageInjected):
                        queue_entry_id = self._persist_queue_injection(
                            session, event, operation_id=operation_id
                        )
                        event = event.model_copy(update={"message_entry_id": queue_entry_id})
                        if not turn_lifecycle.had_tool_calls and not queue_batch_started_new_turn:
                            compaction.queued_turn_started(queue_entry_id)
                            queue_batch_started_new_turn = True
                        skill_event = _queued_skill_invoked(
                            session.session_id, queue_entry_id, event
                        )
                        if skill_event is not None:
                            yield await emit(skill_event)
                    if isinstance(
                        event, MessageCompleted | ToolExecutionEnded
                    ) and completion_event_has_history(event):
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
                        and event.usage.input_tokens > 0
                        and completion_entry_id is not None
                    ):
                        if event.context_observation is not None:
                            self._context_observations[session.session_id] = (
                                event.context_observation
                            )

                    if completion_entry_id is not None:
                        event = event.model_copy(update={"message_entry_id": completion_entry_id})
                    yield await emit(event)
                for compaction_event in compaction.take_events():
                    yield compaction_event
            except ContextOverflowError as exc:
                overflow_error = exc
            except Exception as exc:
                if not saw_loop_error:
                    # Only an open turn gets a failed completion; one the loop already
                    # completed must not receive a second terminal.
                    for terminal_event in turn_lifecycle.terminal_events(
                        str(exc), outcome="failed"
                    ):
                        yield await emit(terminal_event)
                yield await emit(
                    AgentCompleted(
                        session_id=session.session_id,
                        turns=turn_lifecycle.latest_turn,
                        outcome="failed",
                    )
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
                break

            # The primary loop's overflow hook already made the sole retry
            # decision. The loop publishes the overflow's error and closes its
            # turn, and a boundary overflow rolled the prompt back before it was
            # raised, so only the run's own terminal remains here.
            terminal_overflow = compaction.recovery_failure or overflow_error
            if not saw_loop_error:
                for terminal_event in turn_lifecycle.terminal_events(
                    str(terminal_overflow), outcome="failed"
                ):
                    yield await emit(terminal_event)
            yield await emit(
                AgentCompleted(
                    session_id=session.session_id,
                    turns=turn_lifecycle.latest_turn,
                    outcome="failed",
                )
            )
            raise terminal_overflow from None

        self._accepting_queued_messages = False
        auto_compaction_saved = False
        if not compaction.recovered_from_overflow:
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
            turns=turn_lifecycle.latest_turn,
            outcome=terminal_outcome,
        )
        if auto_compaction_status.skip_final_save:
            yield await self._emit_recoverable_event(completed, session=session)
        else:
            yield await emit(completed)

    def _run_tools(
        self,
        registry: ToolRegistry | None,
        operation_tool_names: frozenset[str] | None,
        *,
        context: ToolContext,
    ) -> tuple[tuple[ToolSpec, ...], ConfiguredToolExecutor]:
        """Select the tools one run may use and build the executor that enforces them.

        Args:
            registry (ToolRegistry | None): The run's tool registry, if any.
            operation_tool_names (frozenset[str] | None): Names an operation
                restricts the run to, or None for every tool the mode allows.
            context (ToolContext): Tool context the executor runs tools with.

        Returns:
            tuple[tuple[ToolSpec, ...], ConfiguredToolExecutor]: The specs offered
            to the provider and an executor whose policy allows the same tools.
            Operation names are intersected with the session tool policy,
            because explicit `tools=` specs are not filtered by it.
        """

        effective_tools = self._effective_tools(registry)
        operation_policy: ToolPolicy | None = None
        if operation_tool_names is not None:
            effective_tools = tuple(
                tool for tool in effective_tools if tool.name in operation_tool_names
            )
            allowed_names = {tool.name for tool in effective_tools}
            if registry is not None:
                policy_allowed_names: set[str] = set()
                for name in allowed_names:
                    try:
                        operation_tool = registry.get(name)
                    except UnknownToolError:
                        continue
                    if self.tool_policy.allows(operation_tool):
                        policy_allowed_names.add(name)
                allowed_names = policy_allowed_names
                effective_tools = tuple(
                    tool for tool in effective_tools if tool.name in allowed_names
                )
            operation_policy = ToolPolicy.allow_tool_names(allowed_names)
        executor = ConfiguredToolExecutor(
            registry=registry,
            context=context,
            policy=(
                operation_policy
                if operation_policy is not None
                else self._effective_tool_policy(effective_tools)
            ),
            approval_policy=self.tool_approval_policy,
        )
        return effective_tools, executor

    def _start_run_harness(
        self,
        session: JsonlSession,
        history: Sequence[Message],
        *,
        tools: tuple[ToolSpec, ...],
        executor: ConfiguredToolExecutor,
    ) -> tuple[AgentHarness, bool]:
        """Create a run's harness and restore queues a previous run left for this session.

        Args:
            session (JsonlSession): Session the run appends to.
            history (Sequence[Message]): Conversation history the run resumes.
            tools (tuple[ToolSpec, ...]): Tool specs offered to the provider.
            executor (ConfiguredToolExecutor): Executor enforcing the run's tool policy.

        Returns:
            tuple[AgentHarness, bool]: The harness, and whether retained queue
            messages and modes were restored into it.
        """

        harness = AgentHarness(
            AgentHarnessConfig(
                provider=self.provider,
                tool_executor=executor,
                model=self.model,
                tools=tools,
                max_tool_iterations=self.max_tool_iterations,
                effort=self.effort,
                prompt_cache_key=_prompt_cache_key(session.session_id),
                web_search=self.web_search,
                context_window=self._context_window(),
                context_reserve_tokens=self._effective_context_reserve_tokens(),
                cost_estimator=self._cost_estimator,
            ),
            messages=self._conversation_history(history),
        )
        retained = self._retained_queues.pop(session.session_id, None)
        if retained is None:
            return harness, False
        harness.set_steering_mode(retained.steering_mode)
        harness.set_follow_up_mode(retained.follow_up_mode)
        for message in retained.messages.steering:
            harness.steer_message(message)
        for message in retained.messages.follow_up:
            harness.follow_up_message(message)
        return harness, True

    def _persist_queue_injection(
        self,
        session: JsonlSession,
        event: QueueMessageInjected,
        *,
        operation_id: str | None,
    ) -> str:
        """Queue an injected steering or follow-up message for persistence.

        Args:
            session (JsonlSession): Session the run appends to.
            event (QueueMessageInjected): The harness's injection event.
            operation_id (str | None): Operation owning the entry.

        Returns:
            str: The entry id the message will be persisted under.
        """

        return self._queue_message(
            session,
            Message(
                role="user",
                content=event.content,
                skill_invocation=event.skill_invocation,
                created_at=event.timestamp,
            ),
            operation_id=operation_id,
        )

    async def compact(
        self,
        session: JsonlSession,
        instructions: str | None = None,
    ) -> AsyncIterator[WispEvent]:
        """Summarize an active context prefix and append one durable compaction."""

        async with self._operation_lock:
            self._operation_active = True
            self._active_persistence = RunPersistence(
                session=session,
                expected_active_leaf_id=session.read_active_leaf_id(),
                operation_id=None,
            )
            try:
                replay = await self._prepare_compaction_replay(session)
                self._active_persistence = RunPersistence(
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

        excess_tokens = provider_auto_compaction_excess_tokens(budget)
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

    def _harness_context_budget(
        self,
        harness: AgentHarness,
        *,
        active_from: int | None = None,
    ) -> ContextBudget:
        provider_active_from = (
            active_from if active_from is not None else active_turn_start(harness.messages)
        )
        return build_context_budget(
            estimate_context(
                self._normalize_provider_messages(
                    harness.messages,
                    active_from=provider_active_from,
                ),
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
        active_from: int | None = None,
    ) -> AsyncIterator[WispEvent]:
        provider_active_from = (
            active_from if active_from is not None else active_turn_start(harness.messages)
        )
        provider_messages = self._normalize_provider_messages(
            harness.messages,
            active_from=provider_active_from,
        )
        tools = tuple(harness.config.tools)
        observation = self._context_observations.get(session.session_id)
        budget = estimate_context_budget(
            provider_messages,
            tools,
            context_window=self._context_window(),
            reserve_tokens=self._effective_context_reserve_tokens(),
            observation=observation,
            provider=self.provider.name,
            model=self.model or self.provider.default_model,
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
                # The summary is already durable. Complete deterministic
                # rehydration under a shield so cancellation cannot publish a
                # contradictory "cancelled" compaction without its committed
                # record or leave `will_retry` ambiguous.
                try:
                    with anyio.CancelScope(shield=True):
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
            provider_messages = (
                *(
                    await self._prompt_messages_async(
                        session_id=session.session_id if session is not None else None,
                        session=session,
                    )
                ).messages,
                *self._normalize_provider_messages(history),
            )
            observation = self._context_observations.get(
                session.session_id if session is not None else ""
            )
            observed_tokens = observation.input_tokens if observation is not None else None
            return build_session_stats(
                session_id=session.session_id if session is not None else None,
                entries=entries,
                replay=replay,
                provider_messages=provider_messages,
                tools=self._effective_tools(),
                context_window=self._context_window(),
                reserve_tokens=self._effective_context_reserve_tokens(),
                provider=self.provider.name,
                model=self.model or self.provider.default_model,
                observed_tokens=observed_tokens,
                auto_compaction_enabled=self.auto_compaction_enabled,
            )

    def _web_search_guidance(self) -> str:
        """Return the prompt guidance for the web search setting, or "" when it is off."""

        if not self.web_search:
            return ""
        if provider_supports_web_search(self.provider):
            return WEB_SEARCH_GUIDANCE
        return WEB_SEARCH_UNAVAILABLE_GUIDANCE

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
        session_id: str | None = None,
        session: JsonlSession | None = None,
    ) -> _SessionPrompt:
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
        # Both prompt paths add the same host guidance, in this order.
        additional_guidance = (skill_index, self._web_search_guidance())
        if self.prompt_messages is not None:
            return _SessionPrompt(
                messages=extend_custom_prompt_messages(
                    self.prompt_messages,
                    additional_guidance=additional_guidance,
                    mode=self.mode,
                )
            )
        repository_status = (
            self._repository_status_snapshot(session_id, operation_context.cwd, session=session)
            if self.trusted and session_id is not None
            else None
        )
        messages = build_prompt_messages(
            cwd=operation_context.cwd,
            tools=effective_tools,
            tool_prompt_metadata=effective_registry.prompt_metadata(
                tool.name for tool in effective_tools
            )
            if effective_registry is not None
            else (),
            additional_guidance=additional_guidance,
            mode=self.mode,
            max_context_chars=self.project_context_max_chars,
            include_project_context=self.trusted,
            protected_paths=operation_context.protected_paths,
            trusted_context_root=self.project_context_root,
            repository_status=repository_status,
        )
        return _SessionPrompt(
            messages=messages,
            repository_status=(
                RepositoryStatusSnapshot(
                    cwd=str(operation_context.cwd.resolve(strict=False)),
                    status=repository_status,
                )
                if repository_status
                else None
            ),
        )

    def _repository_status_snapshot(
        self,
        session_id: str,
        cwd: Path,
        *,
        session: JsonlSession | None = None,
    ) -> str:
        """Return the session's Git status snapshot, reading Git on first use.

        A process that has not built a prompt for this session yet (a restart or a
        one-shot CLI run) first recovers the snapshot its earlier runs persisted, so
        the resumed prompt keeps the prefix the provider cached. Git is read when
        that cache cannot apply (see `_persisted_repository_status`).

        Args:
            session_id (str): Session whose runs share the snapshot.
            cwd (Path): Working directory the status describes.
            session (JsonlSession | None): The persisted session to recover from.

        Returns:
            str: The status stored first for this session and directory. Concurrent
                first reads may both run, but every caller gets the one stored value.
        """
        key = (session_id, cwd)
        snapshot = self._repository_status_snapshots.get(key)
        if snapshot is not None:
            return snapshot
        recovered = (
            _persisted_repository_status(
                session,
                cwd,
                provider=self.provider.name,
                model=self.model,
                # The key this run's requests will carry: the loop only sends one to
                # adapters that declare the capability (as it records it).
                prompt_cache_key=(
                    _prompt_cache_key(session.session_id)
                    if getattr(self.provider, "supports_prompt_cache_key", False) is True
                    else None
                ),
            )
            if session is not None
            else None
        )
        # Prompts are built on worker threads that a cancelled run abandons, so an
        # abandoned read can finish after a retry already stored its snapshot. The
        # first stored snapshot wins; a later read never replaces what prompts used.
        return self._repository_status_snapshots.setdefault(
            key, recovered if recovered is not None else read_repository_status(cwd)
        )

    async def _prompt_messages_async(
        self,
        tools: Sequence[ToolSpec] | None = None,
        *,
        registry: ToolRegistry | None = None,
        context: ToolContext | None = None,
        session_id: str | None = None,
        session: JsonlSession | None = None,
    ) -> _SessionPrompt:
        """Build the operation prompt without blocking the event-loop thread."""

        return await anyio.to_thread.run_sync(
            lambda: self._prompt_messages(
                tools,
                registry=registry,
                context=context,
                session_id=session_id,
                session=session,
            ),
            abandon_on_cancel=True,
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
                execution=self.tool_registry.execution_metadata_for(tool.name),
                prompt=self.tool_registry.prompt_metadata_for(tool.name),
            )
        return registry

    def _conversation_history(self, history: Sequence[Message]) -> tuple[Message, ...]:
        """Retain raw tool metadata while replacing stale system prompts."""

        return tuple(message for message in history if message.role != "system")

    def _normalize_provider_messages(
        self,
        messages: Sequence[Message],
        *,
        active_from: int | None = None,
    ) -> tuple[Message, ...]:
        return prepare_provider_history(
            messages,
            provider=self.provider,
            effort=self.effort,
            active_from=active_from,
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
        message = message_from_completion_event(event)
        if isinstance(event, MessageCompleted) and event.response_id is not None:
            # Save the provider's own output items with the row, so a later run in
            # any process can replay this response exactly and keep the prompt
            # cache warm. The public event never carries them.
            native_output = provider_native_output(self.provider, event.response_id)
            if native_output is not None:
                message = message.model_copy(update={"native_output": native_output})
        return self._queue_message(
            session,
            message,
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
            persistence = RunPersistence(
                session=session,
                expected_active_leaf_id=session.read_active_leaf_id(),
                operation_id=operation_id,
            )
        self._pending_entries.append(PendingSessionEntry(persistence=persistence, entry=entry))
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


class _RunCompaction:
    """Compaction policy for one run, serving as the harness's boundary and overflow hooks.

    `CodingSession._run` creates one per run once the prompt's persisted position
    is known, passes it to `AgentHarness.continue_` as both the boundary preparer
    and the context-overflow hook, and reports the run facts this policy depends
    on through the named methods below. Compaction events produced inside a hook
    are buffered until `_run` publishes them with `take_events`, so they keep
    their place in the run's event stream.
    """

    def __init__(
        self,
        owner: CodingSession,
        *,
        session: JsonlSession,
        harness: AgentHarness,
        persistence: RunPersistence,
        operation_id: str | None,
        prompt_messages: Sequence[Message],
        run_entry_start: int,
        run_start_leaf_id: str | None,
        provider_auto_compaction: bool,
        status: _AutoCompactionStatus,
        prompt_entry_id: str,
    ) -> None:
        """Bind the policy to one run.

        Args:
            owner (CodingSession): Session whose compaction operations and
                configuration the hooks use.
            session (JsonlSession): Session the run appends to.
            harness (AgentHarness): Harness executing the run.
            persistence (RunPersistence): The run's branch-pinned writer.
            operation_id (str | None): Operation owning the run's entries.
            prompt_messages (Sequence[Message]): System prompt rows that prefix
                every rebuilt provider transcript.
            run_entry_start (int): Session entry count before the prompt was persisted.
            run_start_leaf_id (str | None): Active leaf before the prompt was persisted.
            provider_auto_compaction (bool): Whether a provider-catalogued limit
                enables compaction at request boundaries.
            status (_AutoCompactionStatus): Auto-compaction status shared with
                the run's preflight and final checks.
            prompt_entry_id (str): Entry of the run's prompt. It starts the
                active turn until a queued message starts a new one.
        """
        self._owner = owner
        self._session = session
        self._harness = harness
        self._persistence = persistence
        self._operation_id = operation_id
        self._prompt_messages = tuple(prompt_messages)
        self._run_entry_start = run_entry_start
        self._run_start_leaf_id = run_start_leaf_id
        self._provider_auto_compaction = provider_auto_compaction
        self._status = status
        self._prompt_entry_id = prompt_entry_id
        self._active_entry_id = prompt_entry_id
        self._had_unsafe_tool_round = False
        self._overflow_recovery_attempted = False
        self._recovered_from_overflow = False
        self._recovery_failure: ContextOverflowError | None = None
        self._events: deque[WispEvent] = deque()

    @property
    def recovered_from_overflow(self) -> bool:
        """Whether the overflow hook compacted and scheduled a retry."""

        return self._recovered_from_overflow

    @property
    def recovery_failure(self) -> ContextOverflowError | None:
        """The overflow hook's failure, which replaces the provider's overflow error."""

        return self._recovery_failure

    def queued_turn_started(self, entry_id: str) -> None:
        """Make an injected queue message the start of the active turn.

        Args:
            entry_id (str): Persisted entry of the queue message that began a
                new turn. Boundary compaction keeps it and everything after it.
        """

        self._active_entry_id = entry_id

    def unsafe_tool_ran(self) -> None:
        """Record that an unsafe tool executed, which rules out overflow retry."""

        self._had_unsafe_tool_round = True

    def take_events(self) -> Iterator[WispEvent]:
        """Drain the compaction events the hooks have published so far.

        Yields:
            WispEvent: Buffered events in the order the hooks produced them.
        """

        while self._events:
            yield self._events.popleft()

    async def rollback_active_prompt(self) -> None:
        """Restore the active branch to its leaf from before the prompt was persisted."""

        session = self._session
        if self._operation_id is not None:
            restored = await session.restore_active_leaf_for_operation(
                self._run_entry_start,
                self._run_start_leaf_id,
                operation_id=self._operation_id,
            )
            if restored:
                self._persistence.expected_active_leaf_id = self._run_start_leaf_id
            return
        current_leaf_id = await anyio.to_thread.run_sync(session.read_active_leaf_id)
        if current_leaf_id != self._run_start_leaf_id:
            await session.select_active_leaf(
                self._run_start_leaf_id,
                expected_active_leaf_id=current_leaf_id,
                operation_id=self._operation_id,
            )
            self._persistence.expected_active_leaf_id = self._run_start_leaf_id

    async def compact_before_first_request(self) -> AsyncIterator[WispEvent]:
        """Compact history before the first request, keeping the prompt's turn active.

        When compaction completes, the harness transcript is rebuilt from the
        compacted session so the first request uses it. The caller checks the
        remaining budget afterwards.

        Yields:
            WispEvent: Compaction events, already published.
        """

        compacted = False
        async for compaction_event in self._owner._maybe_auto_compact(
            self._session,
            self._harness,
            status=self._status,
            operation_id=self._operation_id,
            preflight_active_entry_id=self._prompt_entry_id,
        ):
            if (
                isinstance(compaction_event, CompactionCompleted)
                and compaction_event.outcome == "completed"
            ):
                compacted = True
            yield compaction_event
        if compacted:
            active_history = await anyio.to_thread.run_sync(self._session.read_context_messages)
            self._harness.replace_messages(
                (*self._prompt_messages, *self._owner._conversation_history(active_history))
            )

    async def prepare_boundary(
        self, *, context: HarnessBoundaryContext
    ) -> RequestBoundaryDecision | None:
        """Compact or truncate before the next request when a provider limit applies.

        Args:
            context (HarnessBoundaryContext): Transcript snapshot and queue
                effects of the completed turn.

        Returns:
            RequestBoundaryDecision | None: A replacement or rebase after
            compaction or truncation, or None for the harness's default.

        Raises:
            ContextOverflowError: The active turn stays over the limit after
                compaction and truncation; the prompt is rolled back first.
        """

        try:
            return await self._prepare_boundary(context)
        except ContextOverflowError:
            # A boundary overflow follows an already-completed tool turn. Roll
            # the active branch back to its pre-prompt leaf before the loop
            # publishes this error, so the error is recorded on the restored
            # branch and an irreducible tool result cannot poison later requests.
            await self._owner._flush_pending_entries()
            await self.rollback_active_prompt()
            raise

    async def _prepare_boundary(
        self, context: HarnessBoundaryContext
    ) -> RequestBoundaryDecision | None:
        owner = self._owner
        harness = self._harness
        if context.stop_by_default or not self._provider_auto_compaction:
            return None
        compacted = False
        async for compaction_event in owner._maybe_auto_compact(
            self._session,
            harness,
            status=self._status,
            operation_id=self._operation_id,
            preflight_active_entry_id=self._active_entry_id,
            active_from=context.active_from,
        ):
            self._events.append(compaction_event)
            if (
                isinstance(compaction_event, CompactionCompleted)
                and compaction_event.outcome == "completed"
            ):
                compacted = True
        if compacted:
            return await self._replacement_after_compaction(
                context.snapshot.continuation_messages,
                has_native_continuation=context.snapshot.can_append_user_messages,
                injected_messages=context.injected_messages,
            )

        # A provider catalog limit can leave the active tool result as
        # the only remaining source of excess. This replacement is
        # intentionally fresh: truncating a loop-owned tail is not an
        # additive rebase.
        remaining_budget = owner._harness_context_budget(
            harness,
            active_from=context.active_from,
        )
        if exceeds_provider_auto_compaction_limit(remaining_budget):
            excess_tokens = provider_auto_compaction_excess_tokens(remaining_budget)
            truncated = (
                truncate_active_turn_tool_results(
                    harness.messages,
                    excess_tokens=excess_tokens + excess_tokens // 4 + 64,
                )
                if excess_tokens is not None
                else None
            )
            if truncated is not None:
                return RequestBoundaryDecision(
                    messages=owner._normalize_provider_messages(
                        truncated,
                        active_from=context.active_from,
                    )
                )
            raise ContextOverflowError(
                "Active tool result exceeds the provider auto-compaction limit "
                "after compacting all eligible history"
            )
        return None

    async def recover_context_overflow(
        self, *, snapshot: ContextOverflowSnapshot
    ) -> RequestBoundaryDecision | ContextOverflowFailure | None:
        """Compact once per run so the loop can retry an overflowed request.

        Args:
            snapshot (ContextOverflowSnapshot): The rejected request's turn state
                and context budget.

        Returns:
            RequestBoundaryDecision | ContextOverflowFailure | None: The retry
            replacement after compaction, a failure that replaces the provider's
            message, or None to decline recovery.
        """

        owner = self._owner
        session = self._session
        # The hook is the sole recovery authority for this primary
        # generator. Mark every decision attempt so legacy terminal
        # handling cannot create a second loop/retry after it returns.
        if self._overflow_recovery_attempted:
            return None
        self._overflow_recovery_attempted = True
        if (
            not owner.auto_compaction_enabled
            or snapshot.had_streamed_delta
            or self._had_unsafe_tool_round
            or (
                snapshot.context_budget.context_window is not None
                and snapshot.context_budget.reserve_tokens >= snapshot.context_budget.context_window
            )
        ):
            return None
        replay: SessionReplay | None = None
        try:
            replay = await owner._prepare_compaction_replay(session)
            plan = plan_manual_compaction(replay)
        except NothingToCompactError:
            return None
        except Exception as exc:
            source_entry_count = len(replay.context_entry_ids) if replay is not None else 0
            self._events.append(
                await owner._emit_recoverable_event(
                    CompactionStarted(
                        session_id=session.session_id,
                        reason="overflow",
                        source_entry_count=source_entry_count,
                        trigger_budget=snapshot.context_budget,
                    ),
                    session=session,
                    operation_id=self._operation_id,
                )
            )
            self._events.append(
                await owner._emit_recoverable_event(
                    CompactionCompleted(
                        session_id=session.session_id,
                        reason="overflow",
                        outcome="failed",
                        replaced_entry_count=0,
                        retained_entry_count=source_entry_count,
                        provider=owner.provider.name,
                        model=owner.model or owner.provider.default_model,
                        error=str(exc),
                    ),
                    session=session,
                    operation_id=self._operation_id,
                )
            )
            self._recovery_failure = ContextOverflowError(
                f"Context overflow recovery failed: {exc}"
            )
            return ContextOverflowFailure(str(self._recovery_failure))

        retry_decision: RequestBoundaryDecision | None = None

        async def prepare_retry() -> None:
            nonlocal retry_decision
            retry_decision = await self._replacement_after_compaction(
                snapshot.continuation_messages,
                has_native_continuation=snapshot.has_native_continuation,
            )

        completed: CompactionCompleted | None = None
        async for compaction_event in owner._compact_locked(
            session,
            plan,
            reason="overflow",
            instructions=None,
            trigger_budget=snapshot.context_budget,
            recover_failure=True,
            will_retry=True,
            operation_id=self._operation_id,
            retry_setup=prepare_retry,
        ):
            self._events.append(compaction_event)
            if isinstance(compaction_event, CompactionCompleted):
                completed = compaction_event
        if (
            completed is None
            or completed.outcome != "completed"
            or not completed.will_retry
            or retry_decision is None
        ):
            detail = (
                completed.error
                if completed is not None and completed.error
                else "compaction did not complete"
            )
            self._recovery_failure = ContextOverflowError(
                f"Context overflow recovery failed: {detail}"
            )
            return ContextOverflowFailure(str(self._recovery_failure))
        self._recovered_from_overflow = True
        return retry_decision

    async def _replacement_after_compaction(
        self,
        continuation_messages: Sequence[Message],
        *,
        has_native_continuation: bool,
        injected_messages: Sequence[Message] = (),
    ) -> RequestBoundaryDecision:
        """Rehydrate durable context into the least disruptive loop transition."""

        owner = self._owner
        active_history = await anyio.to_thread.run_sync(self._session.read_context_messages)
        replacement = (*self._prompt_messages, *owner._conversation_history(active_history))
        tail = tuple(continuation_messages)
        injected = tuple(injected_messages)
        portable_base = replacement
        if injected and matches_provider_suffix(replacement, injected):
            portable_base = replacement[: -len(injected)]
        tail_matches = bool(tail) and matches_provider_suffix(portable_base, tail)
        if has_native_continuation and tail_matches:
            supports_rebase = getattr(owner.provider, "supports_context_rebase", False) is True
            if supports_rebase:
                return RequestBoundaryDecision(
                    context_rebase=RequestContextRebase(
                        base_messages=owner._normalize_provider_messages(
                            portable_base[: -len(tail)]
                        ),
                        expected_continuation_messages=tail,
                    ),
                    extra_messages=injected,
                )
        # The loop-owned continuation is the authoritative active boundary.
        # A later injected user row must not make that same-run tool exchange
        # look historical during a fresh replacement.
        replacement_active_from = (
            len(portable_base) - len(tail) if tail_matches else active_turn_start(replacement)
        )
        return RequestBoundaryDecision(
            messages=owner._normalize_provider_messages(
                replacement,
                active_from=replacement_active_from,
            )
        )


def _prompt_cache_key(session_id: str) -> str:
    """Return the stable prompt-cache namespace for one durable session."""

    return f"wisp:{session_id}"


def _persisted_repository_status(
    session: JsonlSession,
    cwd: Path,
    *,
    provider: str,
    model: str | None,
    prompt_cache_key: str | None,
) -> str | None:
    """Recover the Git snapshot the session's latest run sent for ``cwd``, if reusable.

    Reusing an older snapshot only pays off while the provider may still hold the
    cached prefix built from it, so recovery requires the new run to continue the
    same prompt cache as the latest recorded response:

    * the same provider and model (caches are not shared across either);
    * the same prompt-cache key: a clone or fork copies history into a new
      session, whose new key cannot match anything cached for the source;
    * a recent response, within `RESUMED_REPOSITORY_STATUS_MAX_AGE`.

    Only the newest project context on the active path is considered, through
    the snapshot saved with it. In every other case the caller reads Git, so a
    stale status is never shown without a caching benefit.

    Args:
        session (JsonlSession): The persisted session a new run continues.
        cwd (Path): Working directory the new run describes.
        provider (str): Provider the new run sends to.
        model (str | None): Model the new run requests; None for the provider default.
        prompt_cache_key (str | None): Key the new run will send; None when the
            provider takes no key.

    Returns:
        str | None: The snapshot text to reuse verbatim, or None to read Git.
    """
    if not session.path.exists():
        return None
    latest_response: MessageSessionEntry | None = None
    for entry in reversed(session.read_active_path()):
        if not isinstance(entry, MessageSessionEntry):
            continue
        message = entry.message
        if latest_response is None and message.role == "assistant":
            latest_response = entry
        if _is_project_context(message):
            if latest_response is None or not _continues_prompt_cache(
                latest_response,
                session=session,
                provider=provider,
                model=model,
                prompt_cache_key=prompt_cache_key,
            ):
                return None
            # Absent for an untrusted context and for sessions saved before the
            # snapshot was stored; either way the caller reads Git.
            snapshot = entry.repository_status
            if snapshot is None or snapshot.cwd != str(cwd.resolve(strict=False)):
                return None
            return snapshot.status
    return None


def _continues_prompt_cache(
    response: MessageSessionEntry,
    *,
    session: JsonlSession,
    provider: str,
    model: str | None,
    prompt_cache_key: str | None,
) -> bool:
    """Return whether a new run can still hit the cache ``response`` was built in."""

    message = response.message
    cost, observation = message.cost, message.context_observation
    recorded_provider = (cost.provider if cost is not None else None) or (
        observation.provider if observation is not None else None
    )
    if recorded_provider != provider:
        return False
    # Compare what each run asked for (None: the provider's default model). The
    # response's own `model` is whatever the provider reported back.
    recorded_model = cost.requested_model if cost is not None else None
    if recorded_model != model:
        return False
    if utc_now() - response.created_at > RESUMED_REPOSITORY_STATUS_MAX_AGE:
        return False
    recorded_key = observation.prompt_cache_key if observation is not None else None
    if recorded_key is not None or prompt_cache_key is not None:
        # The request recorded the key it was sent with. A response copied in by a
        # clone or fork carries its source's key, so this is exact. A response
        # without a key (an older record, or a keyless provider) cannot match a
        # keyed run, which is always correct to refresh.
        return recorded_key == prompt_cache_key
    # Neither side has a key: a provider that takes none, or a record written
    # before keys were persisted (a `None` key is not serialized, so the two look
    # alike). A copied response predates the clone's file, whose name holds its
    # UTC creation second; this floors, so a same-second copy can still pass.
    created = _session_file_created_at(session.path)
    return created is None or response.created_at >= created


def _session_file_created_at(path: Path) -> datetime | None:
    """Return the UTC creation second `JsonlSessionStore.create` encodes in a file name."""

    stamp = path.name[:15]
    try:
        return datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _queued_skill_invoked(
    session_id: str, entry_id: str, event: QueueMessageInjected
) -> SkillInvoked | None:
    """Return the SkillInvoked event announcing an injected queue message's skill, if any."""

    if event.skill_invocation is None:
        return None
    return SkillInvoked(
        session_id=session_id,
        message_entry_id=entry_id,
        invocation=event.skill_invocation,
        provider_content=event.content,
        queue_kind=event.kind,
    )


def _tool_result_status(event: ToolExecutionEnded) -> ToolPresentationStatus:
    return tool_result_status(
        event.is_error,
        event.exit_code,
        process_state=event.process_state,
    )


__all__ = ["CodingSession", "PERSISTED_SESSION_EVENT_TYPES"]
