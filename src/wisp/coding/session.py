"""Application-facing coding session built on the portable agent harness."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import deque
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Sequence,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import anyio

from wisp.agent.context_budget import (
    build_context_budget,
    estimate_context,
    estimate_context_budget,
    tool_schema_tokens,
)
from wisp.agent.harness import (
    DEFAULT_MAX_PENDING_QUEUE_MESSAGES,
    AgentHarness,
    AgentHarnessConfig,
    HarnessBoundaryContext,
    QueuedMessages,
    queue_full_message,
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
    PromptPart,
    assemble_custom_prompt,
    assemble_prompt,
    read_repository_status,
)
from wisp.coding.stats import RunStats, build_session_stats
from wisp.coding.tool_execution import ConfiguredToolExecutor, ImageStore
from wisp.coding.tool_progress import LiveEventSink
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
    ImagePart,
    MessageCompleted,
    QueueKind,
    QueueMessageInjected,
    QueueMode,
    QueueUpdated,
    RunOutcome,
    RunSetup,
    SessionDirectoryChanged,
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
from wisp.mcp.config import (
    AUTO_ON_DEMAND_SCHEMA_TOKENS,
    DEFAULT_MCP_TOOL_EXPOSURE,
    McpToolExposure,
)
from wisp.mcp.discovery import (
    DISCOVERY_PROMPT,
    DISCOVERY_TOOL_NAMES,
    McpCatalog,
    catalog_guidance,
    discovery_tools,
)
from wisp.messages import (
    CompactionRecord,
    Message,
    completion_event_has_history,
    message_from_completion_event,
    queued_message_images,
)
from wisp.messages.history import active_turn_start
from wisp.providers.base import (
    ContextOverflowError,
    Provider,
    ToolCallResult,
    ToolSpec,
    model_accepts_images,
    prepare_provider_history,
    provider_image_request_byte_budget,
    provider_native_output,
    provider_supports_web_search,
    structured_tool_replacement_support,
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
from wisp.sessions.errors import SessionError, StaleSessionWriterError
from wisp.sessions.images import SessionImageError
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore
from wisp.sessions.replay import (
    SessionReplay,
    directory_change_note,
    replay_session_entries,
    with_directory_notes,
)
from wisp.skills.invocation import expand_skill_invocation
from wisp.skills.models import SkillCatalog
from wisp.skills.prompt import build_skill_index
from wisp.skills.tool import SkillTool
from wisp.tool_presentation import tool_result_status
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.base import ToolExecutionMetadata
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.seen_files import SeenFiles

# A resumed session reuses its persisted Git snapshot only while the provider may
# still hold the cached prefix built from it. After a longer pause the cache has
# expired anyway, so a fresh read costs nothing and avoids a long-stale status.
RESUMED_REPOSITORY_STATUS_MAX_AGE = timedelta(hours=1)

_logger = logging.getLogger(__name__)

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
        # Reopened sessions rebuild a run's closing summary line from its totals.
        "agent.completed",
        # Resuming restores the folder, and the next prompt names a move.
        "session.directory_changed",
    }
)


@dataclass(frozen=True, slots=True)
class _SessionPrompt:
    """System messages for one run, and the Git snapshot their project context used.

    The snapshot is saved with the project-context message so a resumed session
    can send the same prompt again (see `_persisted_repository_status`). The parts
    label each message's text by context source for the context breakdown; they
    are never persisted.
    """

    messages: tuple[Message, ...]
    repository_status: RepositoryStatusSnapshot | None = None
    parts: tuple[tuple[PromptPart, ...], ...] = ()


def _is_project_context(message: Message) -> bool:
    return message.role == "system" and message.content.startswith(f"{PROJECT_CONTEXT_HEADER}\n")


@dataclass(frozen=True, slots=True)
class _RetainedQueueState:
    messages: QueuedMessages
    steering_mode: QueueMode = "one_at_a_time"
    follow_up_mode: QueueMode = "one_at_a_time"


async def _read_message_images(session: JsonlSession, message: Message) -> tuple[bytes, ...] | None:
    """Read a message's images from the store, or None when one cannot be read.

    A missing or damaged image becomes placeholder text for the whole message
    rather than failing the run; the failure is logged so it is not invisible.
    """

    try:
        return tuple([await session.read_image(image) for image in message.images or ()])
    except SessionImageError as exc:
        _logger.warning("Sending image placeholders for session %s: %s", session.session_id, exc)
        return None


def _for_image_support(message: Message, *, takes_images: bool) -> Message:
    """Drop a message's image bytes when the run's model cannot take images."""

    if takes_images or message.image_bytes is None:
        return message
    return message.model_copy(update={"image_bytes": None})


class DirectoryChangeError(ValueError):
    """A working directory a session cannot move to.

    The folder must exist, be a directory, and stay inside the session's project
    root, both as written and after following symbolic links. Moving into another
    project is not supported yet.
    """


def _same_folder(first: Path, second: Path) -> bool:
    """Return whether two existing paths name one folder, as the file system sees it."""

    try:
        return os.path.samefile(first, second)
    except OSError:
        return first == second


def _inside_folder(path: Path, root: Path) -> bool:
    """Return whether existing ``path`` is ``root`` or inside it, by file identity."""

    return any(_same_folder(folder, root) for folder in (path, *path.parents))


@dataclass(frozen=True, slots=True)
class _ResumedHistory:
    """What a run resumes from its session."""

    messages: Sequence[Message]
    active_leaf_id: str | None
    # Entries the session already held; zero for a new session.
    entry_count: int
    # The folder the session last recorded, or None if it records none.
    cwd: Path | None
    # Notes for moves since the last user message, which the prompt carries.
    directory_notes: tuple[str, ...]


class ImageInputUnsupportedError(ValueError):
    """New images that this session's model or provider request cannot carry.

    Raised before anything is stored when the model does not take images, or
    when one message's images alone exceed the provider's per-request budget;
    and before a message is queued when its images no longer fit beside those
    the active run's requests already carry.
    """


def _queued_image_bytes(messages: Iterable[Message]) -> int:
    """Return the image bytes queued messages add to the requests that carry them."""

    return sum(
        image.byte_size
        for message in messages
        if message.image_bytes is not None
        for image in message.images or ()
    )


async def _store_images(session: JsonlSession, images: Sequence[bytes]) -> tuple[ImagePart, ...]:
    """Make each image durable in the session's store, in order.

    Storing comes before the message that references the images exists, so no
    later append can point at an image that was not written.
    """

    return tuple([await session.store_image(image) for image in images])


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


@dataclass(slots=True)
class _HarnessLoopState:
    """What a run has learned from its harness events so far."""

    turn_lifecycle: TurnLifecycle = field(default_factory=TurnLifecycle)
    outcome: RunOutcome = "completed"
    saw_loop_error: bool = False
    overflow_error: ContextOverflowError | None = None
    tool_presentation_statuses: dict[str, ToolPresentationStatus] = field(default_factory=dict)
    started_tool_calls: set[str] = field(default_factory=set)
    queue_batch_started_new_turn: bool = False

    def observe(
        self,
        event: WispEvent,
        *,
        compaction: _RunCompaction,
        tool_is_unsafe: Callable[[str], bool],
    ) -> None:
        """Update the state from one harness event before it is persisted and published.

        A turn started after overflow recovery clears the pending overflow, and
        an unsafe tool that actually started rules out overflow retry.

        Args:
            event (WispEvent): Event from the harness loop.
            compaction (_RunCompaction): The run's compaction hooks.
            tool_is_unsafe (Callable[[str], bool]): Whether the named tool is unsafe.
        """

        self.turn_lifecycle.observe(event)
        if isinstance(event, TurnStarted):
            if compaction.recovered_from_overflow:
                self.overflow_error = None
        elif isinstance(event, ErrorEvent):
            self.saw_loop_error = True
        elif isinstance(event, ContextOverflow):
            self.overflow_error = ContextOverflowError(event.message)
        elif isinstance(event, TurnCompleted):
            self.outcome = event.outcome
            self.queue_batch_started_new_turn = False
        elif isinstance(event, ToolExecutionStarted):
            self.started_tool_calls.add(event.call_id)
        elif isinstance(event, ToolExecutionEnded):
            was_started = event.call_id in self.started_tool_calls
            self.started_tool_calls.discard(event.call_id)
            # A proxied MCP call is judged by the tool it ran, not by `mcp_call`.
            if was_started and tool_is_unsafe(event.target or event.name):
                compaction.unsafe_tool_ran()
        elif isinstance(event, ToolApprovalResolved) and not event.approved:
            self.tool_presentation_statuses[event.call_id] = "denied"


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
        mcp_tool_exposure: McpToolExposure = DEFAULT_MCP_TOOL_EXPOSURE,
        mcp_server_connected: Callable[[str], bool] | None = None,
    ) -> None:
        self.sessions = sessions
        self.events = events
        self.tool_registry = tool_registry
        self.mcp_tool_exposure: McpToolExposure = mcp_tool_exposure
        self._mcp_server_connected = mcp_server_connected
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
        # The run harness that accepted `cancel_run`, so its run reports as cancelled.
        self._cancelled_harness: AgentHarness | None = None
        self._active_session_id: str | None = None
        self._active_persistence: RunPersistence | None = None
        # Image bytes the active run's tool results and queued messages may still
        # add to its requests: the request budget less the images its history
        # already sends and those its queues hold. Earlier tool images stay in later
        # requests (Anthropic replays them each time), so the allowance only shrinks
        # until the history is reloaded or a queued message is removed unsent.
        self._tool_image_allowance = 0
        self._last_session_id: str | None = None
        self._accepting_queued_messages = False
        self._retained_queues: dict[str, _RetainedQueueState] = {}
        # One record per conversation of the files its model has seen. It outlives
        # reconfiguration, which replaces the tool context, and compaction, after
        # which the model can still edit a file it read before.
        self._seen_files: dict[str, SeenFiles] = {}
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
        # The folder Wisp started in. A session that records no folder ran here, so
        # one records its folder only when it starts somewhere else, after a move.
        self._launch_cwd = self.tool_context.cwd.resolve(strict=False)

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
        mcp_tool_exposure: McpToolExposure = DEFAULT_MCP_TOOL_EXPOSURE,
        mcp_server_connected: Callable[[str], bool] | None = None,
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
            mcp_tool_exposure=mcp_tool_exposure,
            mcp_server_connected=mcp_server_connected,
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

    async def follow_up(self, content: str, *, images: Sequence[bytes] = ()) -> QueueUpdated:
        """Queue a user message for the active run's next completed-turn boundary.

        Args:
            content (str): The message text.
            images (Sequence[bytes], optional): Image files attached to the
                message, stored in the active session before it is queued.
                Defaults to ().

        Returns:
            QueueUpdated: The queue state after the message was added.

        Raises:
            RuntimeError: No run accepts queued messages, or it changed meanwhile.
            ImageRejectedError: An image fails Wisp's image checks.
        """

        return await self._queue_user_message("follow_up", content, images)

    async def steer(self, content: str, *, images: Sequence[bytes] = ()) -> QueueUpdated:
        """Queue a user message after the active run's current assistant/tool batch.

        Args:
            content (str): The message text.
            images (Sequence[bytes], optional): Image files attached to the
                message, stored in the active session before it is queued.
                Defaults to ().

        Returns:
            QueueUpdated: The queue state after the message was added.

        Raises:
            RuntimeError: No run accepts queued messages, or it changed meanwhile.
            ImageRejectedError: An image fails Wisp's image checks.
        """

        return await self._queue_user_message("steering", content, images)

    async def _queue_user_message(
        self, kind: QueueKind, content: str, images: Sequence[bytes]
    ) -> QueueUpdated:
        harness = self._active_queue_harness()
        if images:
            self._require_image_input(images)
        stored_images = await _store_images(self._active_run_session(), images) if images else ()
        message = await self._prepare_user_message(
            content, images=stored_images, image_bytes=images
        )
        # Storing and skill expansion await, so the run may have ended meanwhile.
        if harness is not self._active_queue_harness():
            raise RuntimeError("CodingSession active agent run changed")
        # Every later request of the run carries this message's images beside the
        # ones it already sends, so they must fit what is left of its budget; they
        # take that room from its tools until the message leaves the queue unsent.
        image_size = _queued_image_bytes((message,))
        if image_size > self._tool_image_allowance:
            raise ImageInputUnsupportedError(
                f"The attached images total {image_size} bytes, but this run's requests "
                f"have room for {max(self._tool_image_allowance, 0)} more bytes of images. "
                "Send them after the run ends, or attach fewer or smaller images"
            )
        queued = (
            harness.steer_message(message)
            if kind == "steering"
            else harness.follow_up_message(message)
        )
        self._tool_image_allowance -= image_size
        return queued

    @staticmethod
    def queued_message_rejection(queued_messages: int) -> str | None:
        """Return the error a run would give one more steering or follow-up message.

        A frontend that holds messages until a run accepts them checks each one here,
        so it reports the same queue-full error the run reports for its own queue.

        Args:
            queued_messages (int): Steering and follow-up messages already held for
                the run.

        Returns:
            str | None: The queue-full error, or None when one more message fits.
        """
        if queued_messages >= DEFAULT_MAX_PENDING_QUEUE_MESSAGES:
            return queue_full_message(DEFAULT_MAX_PENDING_QUEUE_MESSAGES)
        return None

    def cancel_run(self) -> bool:
        """Ask the active run to stop at its next cancellation point.

        The run then ends the way a requested cancellation does: running tools
        are settled, an open turn closes as cancelled, and the run still saves
        and publishes `AgentCompleted` with outcome "cancelled" and its totals.
        Cancelling the caller's task or scope instead skips that ending. This
        stops whichever run the session has active; runs on one session do not
        overlap, so a caller targets its own run by asking while it is running.

        Returns:
            bool: True when an active run accepted the request. False before the
            run's loop starts, while it prepares its prompt, or after it ends, so
            a caller that must stop a run retries until it is accepted.
        """

        harness = self._active_harness
        if harness is None or not harness.cancel():
            return False
        self._cancelled_harness = harness
        return True

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
        if message is not None:
            self._tool_image_allowance += _queued_image_bytes((message,))
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
        self._tool_image_allowance += _queued_image_bytes((*cleared.steering, *cleared.follow_up))
        return cleared, harness.queue_updated_event()

    def _active_queue_harness(self) -> AgentHarness:
        harness = self._active_harness
        if harness is None or not self._accepting_queued_messages:
            raise RuntimeError("CodingSession has no active agent run")
        return harness

    def _active_run_session(self) -> JsonlSession:
        persistence = self._active_persistence
        if persistence is None:
            raise RuntimeError("CodingSession has no active agent run")
        return persistence.session

    async def _prepare_user_message(
        self,
        content: str,
        *,
        images: tuple[ImagePart, ...] = (),
        image_bytes: Sequence[bytes] = (),
        context: ToolContext | None = None,
    ) -> Message:
        expanded, evidence = await expand_skill_invocation(
            content,
            catalog=self.skill_catalog,
            context=context or self.tool_context,
        )
        return Message(
            role="user",
            content=expanded,
            images=images or None,
            image_bytes=tuple(image_bytes) if images else None,
            skill_invocation=evidence,
        )

    def _model_takes_images(self) -> bool:
        """Return whether this session's provider and model accept image input.

        The adapter must opt in, and when a model catalog is configured it must
        list the model; without one the adapter's declaration decides.
        """

        return model_accepts_images(self.provider, self.model, self.models)

    def _require_image_input(self, images: Sequence[bytes]) -> None:
        """Refuse new images before anything is stored when no request could carry them.

        Older images in history are trimmed to fit the request budget, but a new
        message's own images must all reach the model, so a set larger than the
        budget is refused rather than sent to a provider that would reject it.
        """

        model = self.model or self.provider.default_model
        if not self._model_takes_images():
            raise ImageInputUnsupportedError(
                f"{self.provider.name} model {model!r} does not accept images; "
                "switch to a model that does, or send text only"
            )
        total = sum(len(image) for image in images)
        budget = provider_image_request_byte_budget(self.provider)
        if total > budget:
            raise ImageInputUnsupportedError(
                f"The attached images total {total} bytes; one {self.provider.name} request "
                f"carries at most {budget} bytes of images. Attach fewer or smaller images"
            )

    async def _with_image_bytes(
        self,
        session: JsonlSession,
        messages: Sequence[Message],
        *,
        reserved_bytes: int = 0,
    ) -> tuple[Message, ...]:
        """Give a request's image-bearing messages the bytes this model can take.

        Images are sent newest first until the adapter's per-request image budget
        is spent; older ones, every image for a model that does not take images,
        and images missing from the store go as placeholder text instead, so a
        long session never grows past what the provider accepts.

        Args:
            session (JsonlSession): Session whose image store holds the images.
            messages (Sequence[Message]): Transcript about to be sent.
            reserved_bytes (int, optional): Budget already taken by images the
                request adds after these messages. Defaults to 0.

        Returns:
            tuple[Message, ...]: The messages in order; each image-bearing one
            carries bytes for all of its images, or none.
        """

        takes_images = self._model_takes_images()
        remaining = provider_image_request_byte_budget(self.provider) - reserved_bytes
        prepared = list(messages)
        # A provider that replays earlier tool calls as an assistant-role summary
        # cannot carry their images, so loading their bytes would waste budget.
        replays_tool_images = (
            structured_tool_replacement_support(self.provider, effort=self.effort) is True
        )
        for index in range(len(prepared) - 1, -1, -1):
            message = prepared[index]
            if message.images is None or (message.role == "tool" and not replays_tool_images):
                continue
            size = sum(image.byte_size for image in message.images)
            image_bytes = None
            if takes_images and size <= remaining:
                image_bytes = message.image_bytes or await _read_message_images(session, message)
            if image_bytes is not None:
                remaining -= size
            if image_bytes != message.image_bytes:
                prepared[index] = message.model_copy(update={"image_bytes": image_bytes})
        return tuple(prepared)

    def _start_tool_image_allowance(
        self, history: Sequence[Message], *, reserved_bytes: int = 0
    ) -> None:
        """Let the active run's tool images use the budget its history leaves.

        Args:
            history (Sequence[Message]): The run's history with its bytes loaded.
            reserved_bytes (int, optional): Budget taken by images the run's
                requests add after ``history``: the prompt's, or those of messages
                still queued. Defaults to 0.
        """

        sent = sum(
            image.byte_size
            for message in history
            if message.image_bytes is not None
            for image in message.images or ()
        )
        self._tool_image_allowance = (
            provider_image_request_byte_budget(self.provider) - reserved_bytes - sent
        )

    async def _load_tool_result_images(
        self, session: JsonlSession, results: Sequence[ToolCallResult]
    ) -> tuple[ToolCallResult, ...]:
        """Give a tool batch's images the bytes the next request can carry.

        Results are taken in batch order while the run's allowance lasts; an image
        the model cannot take, that does not fit, or that is missing from the store
        goes as placeholder text instead.

        Args:
            session (JsonlSession): Session whose image store holds the images.
            results (Sequence[ToolCallResult]): The batch's results, in order.

        Returns:
            tuple[ToolCallResult, ...]: The results, each image-bearing one with
            bytes for all of its images or none.
        """

        takes_images = self._model_takes_images()
        loaded: list[ToolCallResult] = []
        for result in results:
            size = sum(image.byte_size for image in result.images)
            if not result.images or not takes_images or size > self._tool_image_allowance:
                loaded.append(result)
                continue
            try:
                data = tuple([await session.read_image(image) for image in result.images])
            except SessionImageError as exc:
                _logger.warning(
                    "Sending tool image placeholders for session %s: %s", session.session_id, exc
                )
                loaded.append(result)
                continue
            self._tool_image_allowance -= size
            loaded.append(replace(result, image_bytes=data))
        return tuple(loaded)

    @staticmethod
    def _queue_updated_from_snapshot(queued: _RetainedQueueState | None) -> QueueUpdated:
        if queued is None:
            return QueueUpdated()
        return QueueUpdated(
            steering=tuple(message.user_visible_content for message in queued.messages.steering),
            follow_up=tuple(message.user_visible_content for message in queued.messages.follow_up),
            steering_images=queued_message_images(queued.messages.steering),
            follow_up_images=queued_message_images(queued.messages.follow_up),
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

    async def change_directory(
        self, path: str, *, session: JsonlSession | None
    ) -> SessionDirectoryChanged | None:
        """Move the working directory to another folder in the same project.

        The tools work from the new folder from the next operation on. When
        ``session`` already has entries, the move is saved in it, so resuming the
        session restores the folder and the next prompt tells the model; a session
        that has none records its folder when its first prompt runs.

        Args:
            path (str): The folder: absolute, ``~``, or relative to the current one.
            session (JsonlSession | None): The session to record the move in.

        Returns:
            SessionDirectoryChanged | None: The saved move, or None when nothing
            was saved because the folder did not change or the session is empty.

        Raises:
            RuntimeError: While a prompt, compaction, or another folder change runs.
            DirectoryChangeError: When the folder does not exist, is not a
                directory, or lies outside the project root.
            SessionError: When the move cannot be saved; the folder is unchanged.
            OSError: When the session file cannot be written; the folder is unchanged.
        """

        async with self._exclusive_operation():
            target = self._directory_in_project(path)
            previous = self.tool_context.cwd.resolve(strict=False)
            if _same_folder(target, previous):
                return None
            change = None
            if session is not None and session.path.exists():
                change = SessionDirectoryChanged(
                    session_id=session.session_id, previous_cwd=previous, cwd=target
                )
                # Saved before the folder changes, so a failed save leaves the
                # session where it was rather than moved with no record.
                await self._flush_pending_entries()
                await session.append_event(change)
            self.tool_context = replace(self.tool_context, cwd=target)
            return change

    async def restore_directory(self, session: JsonlSession | None) -> str | None:
        """Work from the folder ``session`` last recorded, or from the launch folder.

        Called when a session, or a branch of it, becomes the active one. A session
        that records no folder ran in the folder Wisp started in, so it gets that
        one; so does a session whose recorded folder can no longer be used, or that
        cannot be read.

        Args:
            session (JsonlSession | None): The session that became active.

        Returns:
            str | None: Why the recorded folder was not restored, or None when it
            was, or when ``session`` records none.

        Raises:
            RuntimeError: While a prompt, compaction, or another folder change runs.
        """

        async with self._exclusive_operation():
            target = self._launch_cwd
            notice = None
            try:
                recorded = await self._recorded_directory(session)
            except (SessionError, OSError) as exc:
                recorded = None
                notice = f"Working in {target}: could not read the session's folder: {exc}"
            if recorded is not None:
                try:
                    target = self._directory_in_project(str(recorded))
                except DirectoryChangeError as exc:
                    notice = f"Working in {target} instead of the session's folder: {exc}"
            self.tool_context = replace(self.tool_context, cwd=target)
            return notice

    async def _recorded_directory(self, session: JsonlSession | None) -> Path | None:
        if session is None or not session.path.exists():
            return None
        snapshot = await anyio.to_thread.run_sync(session.read_run_snapshot)
        return snapshot.replay.cwd

    @asynccontextmanager
    async def _exclusive_operation(self) -> AsyncIterator[None]:
        """Hold the operation lock for a short change, failing instead of waiting.

        A prompt or compaction holds the same lock for its whole run, so a folder
        change can never interleave with one, even for a direct caller that the RPC
        host does not order.
        """

        try:
            self._operation_lock.acquire_nowait()
        except anyio.WouldBlock:
            raise RuntimeError("CodingSession is busy") from None
        try:
            self._operation_active = True
            yield
        finally:
            self._operation_active = False
            self._operation_lock.release()

    def _directory_in_project(self, path: str) -> Path:
        """Resolve ``path`` from the working directory and check it stays in the project.

        The folder's real path, after symbolic links, must lie inside the real
        project root, so a link inside the project cannot lead out. Containment is
        checked by file identity, so case-insensitive file systems compare folders
        the way they open them.
        """

        if "\0" in path:
            # The path functions below raise ValueError for it, which no caller expects.
            raise DirectoryChangeError(f"Path contains a NUL character: {path!r}")
        root = self.project_context_root
        if root is None:
            raise DirectoryChangeError("This session has no project root to move within")
        real_root = Path(os.path.realpath(root))
        cwd = self.tool_context.cwd.resolve(strict=False)
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = cwd / candidate
        # Only where the folder really is matters: a path through a symlinked parent
        # such as /tmp, or spelled in another case, still names a folder inside.
        real = Path(os.path.realpath(candidate))
        if not real.exists():
            raise DirectoryChangeError(f"No such folder: {path}")
        if not real.is_dir():
            raise DirectoryChangeError(f"Not a folder: {path}")
        if not _inside_folder(real, real_root):
            raise DirectoryChangeError(
                f"{path} is outside the project {real_root}; moving into another "
                "project is not supported yet"
            )
        return real

    async def run(
        self,
        prompt: str,
        *,
        images: Sequence[bytes] = (),
        session: JsonlSession | None = None,
        history: Sequence[Message] = (),
        operation_id: str | None = None,
        tool_context: ToolContext | None = None,
        operation_instructions: str | None = None,
        operation_tool_names: frozenset[str] | None = None,
        operation_ready: Callable[[], Awaitable[None]] | None = None,
        live_event_sink: LiveEventSink | None = None,
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
            images (Sequence[bytes], optional): Image files attached to the
                prompt, stored in the session before the prompt is recorded.
                Defaults to ().
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
            live_event_sink (LiveEventSink | None, optional): Receives transient
                `ToolExecutionProgress` events while tools run. They are not
                yielded, persisted, or sent to the model, and each arrives between
                its call's `ToolExecutionStarted` and `ToolExecutionEnded`. The
                sink may wait while its frontend catches up; the tool keeps running
                and newer progress replaces what was not delivered. Defaults to None.

        Yields:
            WispEvent: The run's events in publication order. A `QueueUpdated`
            for restored queues may precede `AgentStarted`; a run that reaches
            its agent loop ends with `AgentCompleted`.

        Raises:
            ContextOverflowError: The prompt cannot fit the provider's context, or
                overflow recovery failed; raised after `AgentCompleted(failed)`.
            ImageRejectedError: An attached image fails Wisp's image checks;
                raised before any event.
            Exception: A failure inside the agent loop propagates after the open
                turn is completed as failed and `AgentCompleted(failed)` is
                published. A failure while preparing the run propagates without
                a terminal event.
        """

        async with self._operation_lock:
            self._operation_active = True
            events = self._run(
                prompt,
                images=images,
                session=session,
                history=history,
                operation_id=operation_id,
                tool_context=tool_context,
                operation_instructions=operation_instructions,
                operation_tool_names=operation_tool_names,
                operation_ready=operation_ready,
                live_event_sink=live_event_sink,
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
                    self._cancelled_harness = None
                    self._active_session_id = None
                    self._active_persistence = None
                    self._operation_active = False

    async def _run(
        self,
        prompt: str,
        *,
        images: Sequence[bytes] = (),
        session: JsonlSession | None = None,
        history: Sequence[Message] = (),
        operation_id: str | None = None,
        tool_context: ToolContext | None = None,
        operation_instructions: str | None = None,
        operation_tool_names: frozenset[str] | None = None,
        operation_ready: Callable[[], Awaitable[None]] | None = None,
        live_event_sink: LiveEventSink | None = None,
    ) -> AsyncGenerator[WispEvent, None]:
        operation_context = tool_context or self.tool_context
        if images:
            self._require_image_input(images)
        session = session or self.sessions.create()
        assert session is not None
        user_message = await self._prepare_user_message(
            prompt,
            images=await _store_images(session, images),
            image_bytes=images,
            context=operation_context,
        )
        self._active_session_id = session.session_id
        self._last_session_id = session.session_id
        if operation_context.seen_files is not None:
            seen_files = self._seen_files.setdefault(session.session_id, SeenFiles())
            operation_context = replace(operation_context, seen_files=seen_files)
        prompt_image_bytes = sum(image.byte_size for image in user_message.images or ())
        resumed = await self._resume_history(
            session, history, reserved_image_bytes=prompt_image_bytes
        )
        history = resumed.messages
        self._start_tool_image_allowance(history, reserved_bytes=prompt_image_bytes)
        persistence = RunPersistence(
            session=session,
            expected_active_leaf_id=resumed.active_leaf_id,
            operation_id=operation_id,
        )
        self._active_persistence = persistence

        run_stats = RunStats()

        async def emit(event: WispEvent) -> WispEvent:
            published = await self._emit(event, session=session, operation_id=operation_id)
            run_stats.observe(published)
            return published

        # The prompt carries a note for every move since the last user message, the
        # same notes a later replay of the saved prompt adds, so both match exactly.
        directory_notes = resumed.directory_notes
        directory_change = self._run_directory_change(session, resumed)
        if directory_change is not None:
            # Saved, not published: the run's own events already name its folder,
            # and a frontend learns of a /cd move from that command's reply.
            await self._flush_pending_entries()
            await persistence.append_event(directory_change)
            if directory_change.previous_cwd is not None and history:
                directory_notes = (*directory_notes, directory_change_note(directory_change.cwd))

        operation_registry = self._operation_tool_registry()
        effective_tools, executor = self._run_tools(
            operation_registry,
            operation_tool_names,
            context=operation_context,
            progress_sink=self._progress_sink(live_event_sink),
            image_store=session.store_image,
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
            mcp_catalog=executor.mcp_catalog,
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
            AgentStarted(
                session_id=session.session_id,
                message_entry_id=user_entry.id,
                setup=_run_setup(effective_tools, prompt_messages),
            )
        )

        await self._persist_run_prompt(
            persistence,
            prompt_messages,
            user_entry,
            repository_status=session_prompt.repository_status,
        )
        # Add the persisted prompt before checking the threshold so provider limits
        # apply to the complete next request, not only the resumed history.
        harness.append_message(with_directory_notes(user_message, directory_notes))
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
            async for event in self._fit_first_request(
                compaction,
                harness,
                resumed=bool(history),
                emit=emit,
                run_stats=run_stats,
                session_id=session.session_id,
            ):
                yield event
            # Steering queued while preflight compaction was running must affect
            # the first provider request, not wait for that request to finish.
            self._accepting_queued_messages = False
            try:
                async for event in self._steer_first_request(
                    compaction,
                    harness,
                    session=session,
                    operation_id=operation_id,
                    emit=emit,
                    run_stats=run_stats,
                ):
                    yield event
            finally:
                self._accepting_queued_messages = True

        loop = _HarnessLoopState()
        harness_events = harness.continue_(
            boundary_preparer=compaction,
            context_overflow_hook=compaction,
        )
        try:
            async for event in harness_events:
                for compaction_event in compaction.take_events():
                    yield compaction_event
                loop.observe(event, compaction=compaction, tool_is_unsafe=self._tool_is_unsafe)
                for published in self._persist_loop_event(
                    event,
                    loop,
                    session=session,
                    operation_id=operation_id,
                    compaction=compaction,
                    tool_cwd=operation_context.cwd,
                ):
                    yield await emit(published)
            for compaction_event in compaction.take_events():
                yield compaction_event
        except ContextOverflowError as exc:
            loop.overflow_error = exc
        except Exception as exc:
            for terminal_event in self._failed_run_terminals(
                loop, exc, run_stats, session_id=session.session_id
            ):
                yield await emit(terminal_event)
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

        if self._cancelled_harness is harness and loop.outcome == "completed":
            # A cancel accepted between turns closes no turn, so the last turn
            # still reads as completed although the run stopped early.
            loop.outcome = "cancelled"
        if loop.overflow_error is not None:
            # The primary loop's overflow hook already made the sole retry
            # decision. The loop publishes the overflow's error and closes its
            # turn, and a boundary overflow rolled the prompt back before it was
            # raised, so only the run's own terminal remains here.
            terminal_overflow = compaction.recovery_failure or loop.overflow_error
            for terminal_event in self._failed_run_terminals(
                loop, terminal_overflow, run_stats, session_id=session.session_id
            ):
                yield await emit(terminal_event)
            raise terminal_overflow from None

        self._accepting_queued_messages = False
        async for event in self._finish_run(
            compaction,
            harness,
            loop,
            session=session,
            operation_id=operation_id,
            status=auto_compaction_status,
            emit=emit,
            run_stats=run_stats,
        ):
            yield event

    async def _resume_history(
        self,
        session: JsonlSession,
        history: Sequence[Message],
        *,
        reserved_image_bytes: int = 0,
    ) -> _ResumedHistory:
        """Choose the history a run resumes and read the leaf its writer is pinned to.

        Entries still pending from earlier runs are flushed first, so the
        snapshot includes them.

        Args:
            session (JsonlSession): Session the run appends to.
            history (Sequence[Message]): History the caller supplied.
            reserved_image_bytes (int, optional): Image budget the new prompt
                already uses, so history images are trimmed around it. Defaults to 0.

        Returns:
            _ResumedHistory: The persisted history when the session has entries,
            had entries pending, or is marked for a history refresh, otherwise
            `history`; the session's active leaf; and the folder and pending
            directory notes it records.
        """

        recover_history = session.session_id in self._history_refresh_session_ids or any(
            pending.entry.session_id == session.session_id for pending in self._pending_entries
        )
        await self._flush_pending_entries()
        snapshot = await anyio.to_thread.run_sync(session.read_run_snapshot)
        if snapshot.entry_count or recover_history:
            history = snapshot.replay.messages
            self._history_refresh_session_ids.discard(session.session_id)
        return _ResumedHistory(
            messages=await self._with_image_bytes(
                session, history, reserved_bytes=reserved_image_bytes
            ),
            active_leaf_id=snapshot.active_leaf_id,
            entry_count=snapshot.entry_count,
            cwd=snapshot.replay.cwd,
            directory_notes=snapshot.replay.pending_directory_notes,
        )

    def _run_directory_change(
        self, session: JsonlSession, resumed: _ResumedHistory
    ) -> SessionDirectoryChanged | None:
        """Return the folder record a run must save before its prompt, if any.

        A session that records no folder ran in the launch folder. A new session
        that starts elsewhere, after a move, records its folder; an older one that
        resumes elsewhere records the move. A session that records a folder and
        runs in another, such as one continued from a different folder, records
        that move too.
        """

        current = self.tool_context.cwd.resolve(strict=False)
        if resumed.cwd is None:
            if current == self._launch_cwd:
                return None
            if not resumed.entry_count:
                return SessionDirectoryChanged(session_id=session.session_id, cwd=current)
            return SessionDirectoryChanged(
                session_id=session.session_id, previous_cwd=self._launch_cwd, cwd=current
            )
        if resumed.cwd == current:
            return None
        return SessionDirectoryChanged(
            session_id=session.session_id, previous_cwd=resumed.cwd, cwd=current
        )

    async def _persist_run_prompt(
        self,
        persistence: RunPersistence,
        prompt_messages: Sequence[Message],
        user_entry: MessageSessionEntry,
        *,
        repository_status: RepositoryStatusSnapshot | None,
    ) -> None:
        """Append a run's system prompt messages, then its user prompt entry.

        Args:
            persistence (RunPersistence): The run's branch-pinned writer.
            prompt_messages (Sequence[Message]): System messages the run sends first.
            user_entry (MessageSessionEntry): Entry for the run's user prompt.
            repository_status (RepositoryStatusSnapshot | None): Git snapshot the
                project-context message was built from, saved with that message.
        """

        for prompt_message in prompt_messages:
            # The project-context entry also carries its Git snapshot, which
            # `append_message` has no way to attach, so it is built here.
            if repository_status is not None and _is_project_context(prompt_message):
                await persistence.append_entry(
                    MessageSessionEntry(
                        session_id=persistence.session.session_id,
                        message=prompt_message,
                        operation_id=persistence.operation_id,
                        repository_status=repository_status,
                    )
                )
            else:
                await persistence.append_message(prompt_message)
        await persistence.append_entry(user_entry)

    async def _fit_first_request(
        self,
        compaction: _RunCompaction,
        harness: AgentHarness,
        *,
        resumed: bool,
        emit: Callable[[WispEvent], Awaitable[WispEvent]],
        run_stats: RunStats,
        session_id: str,
    ) -> AsyncIterator[WispEvent]:
        """Compact resumed history until the first request fits the provider limit.

        Args:
            compaction (_RunCompaction): The run's compaction hooks.
            harness (AgentHarness): Harness holding the first request's transcript.
            resumed (bool): Whether the run resumes history that could be compacted.
            emit (Callable[[WispEvent], Awaitable[WispEvent]]): Publishes a run event.
            run_stats (RunStats): The run's statistics, for its failed completion.
            session_id (str): Session the run appends to.

        Yields:
            WispEvent: Compaction events, then the rejection's `ErrorEvent` and
            failed `AgentCompleted` if the request still does not fit.

        Raises:
            ContextOverflowError: The prompt exceeds the provider auto-compaction
                limit after compacting all eligible history.
        """

        if resumed:
            async for event in compaction.compact_before_first_request():
                yield event
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
            async for event in self._reject_first_request(
                "Active prompt exceeds the provider auto-compaction limit "
                "after compacting all eligible history",
                compaction=compaction,
                emit=emit,
                run_stats=run_stats,
                session_id=session_id,
            ):
                yield event

    async def _steer_first_request(
        self,
        compaction: _RunCompaction,
        harness: AgentHarness,
        *,
        session: JsonlSession,
        operation_id: str | None,
        emit: Callable[[WispEvent], Awaitable[WispEvent]],
        run_stats: RunStats,
    ) -> AsyncIterator[WispEvent]:
        """Add steering queued during preflight to the first request, then recheck its limit.

        The caller stops accepting queued messages while this runs, so steering
        sent meanwhile is rejected rather than missing the first request.

        Args:
            compaction (_RunCompaction): The run's compaction hooks.
            harness (AgentHarness): Harness holding the queued steering.
            session (JsonlSession): Session the run appends to.
            operation_id (str | None): Operation owning the run's entries.
            emit (Callable[[WispEvent], Awaitable[WispEvent]]): Publishes a run event.
            run_stats (RunStats): The run's statistics, for its failed completion.

        Yields:
            WispEvent: The drained queue events, each injected message preceded
            by the `SkillInvoked` it announces; then, if steering was injected,
            compaction events and any rejection.

        Raises:
            ContextOverflowError: The prompt and steering exceed the provider
                auto-compaction limit after compacting all eligible history.
        """

        steering_injected = False
        for queue_event in harness.drain_steering():
            if isinstance(queue_event, QueueMessageInjected):
                steering_injected = True
                queue_entry_id = self._persist_queue_injection(
                    session, queue_event, operation_id=operation_id
                )
                queue_event = queue_event.model_copy(update={"message_entry_id": queue_entry_id})
                skill_event = _queued_skill_invoked(session.session_id, queue_entry_id, queue_event)
                if skill_event is not None:
                    yield await emit(skill_event)
            yield await emit(queue_event)
        if not steering_injected:
            return
        async for event in compaction.compact_before_first_request():
            yield event
        if exceeds_provider_auto_compaction_limit(self._harness_context_budget(harness)):
            async for event in self._reject_first_request(
                "Active prompt and steering exceed the provider "
                "auto-compaction limit after compacting all eligible history",
                compaction=compaction,
                emit=emit,
                run_stats=run_stats,
                session_id=session.session_id,
            ):
                yield event

    async def _reject_first_request(
        self,
        message: str,
        *,
        compaction: _RunCompaction,
        emit: Callable[[WispEvent], Awaitable[WispEvent]],
        run_stats: RunStats,
        session_id: str,
    ) -> AsyncIterator[WispEvent]:
        """Roll back a prompt that cannot fit and fail the run before its first turn.

        Args:
            message (str): Why the prompt cannot be sent.
            compaction (_RunCompaction): The run's compaction hooks.
            emit (Callable[[WispEvent], Awaitable[WispEvent]]): Publishes a run event.
            run_stats (RunStats): The run's statistics, for its failed completion.
            session_id (str): Session the run appends to.

        Yields:
            WispEvent: An `ErrorEvent` with `message`, then a failed
            `AgentCompleted` with no turns.

        Raises:
            ContextOverflowError: Always, with `message`, after both events.
        """

        await compaction.rollback_active_prompt()
        yield await emit(ErrorEvent(message=message))
        yield await emit(
            self._run_completed(run_stats, session_id=session_id, turns=0, outcome="failed")
        )
        raise ContextOverflowError(message)

    def _persist_loop_event(
        self,
        event: WispEvent,
        loop: _HarnessLoopState,
        *,
        session: JsonlSession,
        operation_id: str | None,
        compaction: _RunCompaction,
        tool_cwd: Path,
    ) -> tuple[WispEvent, ...]:
        """Queue the entry a harness event adds to history and return what to publish.

        An injected queue message that opens a new turn, rather than continuing
        a tool round or an earlier message in the same batch, becomes the start
        of the active turn for boundary compaction.

        Args:
            event (WispEvent): Event from the harness loop.
            loop (_HarnessLoopState): The run's loop state, already updated
                for `event`.
            session (JsonlSession): Session the run appends to.
            operation_id (str | None): Operation owning the run's entries.
            compaction (_RunCompaction): The run's compaction hooks.
            tool_cwd (Path): Working directory the run's tools used.

        Returns:
            tuple[WispEvent, ...]: The events to publish in order: `event`,
            carrying the id of the entry it was queued under when it has one,
            preceded by the `SkillInvoked` an injected queue message announces.
        """

        if isinstance(event, QueueMessageInjected):
            entry_id = self._persist_queue_injection(session, event, operation_id=operation_id)
            injected = event.model_copy(update={"message_entry_id": entry_id})
            if not loop.turn_lifecycle.had_tool_calls and not loop.queue_batch_started_new_turn:
                compaction.queued_turn_started(entry_id)
                loop.queue_batch_started_new_turn = True
            skill_event = _queued_skill_invoked(session.session_id, entry_id, injected)
            return (injected,) if skill_event is None else (skill_event, injected)
        if not isinstance(
            event, MessageCompleted | ToolExecutionEnded
        ) or not completion_event_has_history(event):
            return (event,)
        tool_status = (
            loop.tool_presentation_statuses.pop(event.call_id, None)
            if isinstance(event, ToolExecutionEnded)
            else None
        )
        entry_id = self._queue_completion(
            session,
            event,
            operation_id=operation_id,
            tool_status=tool_status,
            tool_cwd=tool_cwd,
        )
        if (
            isinstance(event, MessageCompleted)
            and event.finish_reason not in {"error", "cancelled"}
            and event.usage is not None
            and event.usage.input_tokens > 0
            and event.context_observation is not None
        ):
            self._context_observations[session.session_id] = event.context_observation
        return (event.model_copy(update={"message_entry_id": entry_id}),)

    def _failed_run_terminals(
        self,
        loop: _HarnessLoopState,
        error: Exception,
        run_stats: RunStats,
        *,
        session_id: str,
    ) -> Iterator[WispEvent]:
        """Build the events that end a run whose harness loop failed.

        Only an open turn gets a failed completion, and only when the loop has
        not already published an error; a turn the loop completed must not
        receive a second terminal. The events are built lazily, so the run
        completion counts the turn terminals published before it.

        Args:
            loop (_HarnessLoopState): The run's loop state.
            error (Exception): Why the run failed.
            run_stats (RunStats): The run's statistics.
            session_id (str): Session the run appends to.

        Yields:
            WispEvent: The turn terminals, if any, then a failed `AgentCompleted`.
        """

        if not loop.saw_loop_error:
            yield from loop.turn_lifecycle.terminal_events(str(error), outcome="failed")
        yield self._run_completed(
            run_stats,
            session_id=session_id,
            turns=loop.turn_lifecycle.latest_turn,
            outcome="failed",
        )

    async def _finish_run(
        self,
        compaction: _RunCompaction,
        harness: AgentHarness,
        loop: _HarnessLoopState,
        *,
        session: JsonlSession,
        operation_id: str | None,
        status: _AutoCompactionStatus,
        emit: Callable[[WispEvent], Awaitable[WispEvent]],
        run_stats: RunStats,
    ) -> AsyncIterator[WispEvent]:
        """Compact after a run's last turn if needed, then save and complete the run.

        Args:
            compaction (_RunCompaction): The run's compaction hooks.
            harness (AgentHarness): Harness holding the run's transcript.
            loop (_HarnessLoopState): The finished loop's state.
            session (JsonlSession): Session the run appends to.
            operation_id (str | None): Operation owning the run's entries.
            status (_AutoCompactionStatus): Auto-compaction status for the run.
            emit (Callable[[WispEvent], Awaitable[WispEvent]]): Publishes a run event.
            run_stats (RunStats): The run's statistics.

        Yields:
            WispEvent: Threshold compaction events unless the run recovered from
            an overflow, `SessionSaved` unless compaction already saved or the
            final save is skipped, then `AgentCompleted` with the loop's outcome.
        """

        auto_compaction_saved = False
        if not compaction.recovered_from_overflow:
            async for compaction_event in self._maybe_auto_compact(
                session,
                harness,
                status=status,
                operation_id=operation_id,
            ):
                if isinstance(compaction_event, SessionSaved):
                    auto_compaction_saved = True
                yield compaction_event
        if not auto_compaction_saved and not status.skip_final_save:
            yield await emit(SessionSaved(session_id=session.session_id, path=session.path))
        completed = self._run_completed(
            run_stats,
            session_id=session.session_id,
            turns=loop.turn_lifecycle.latest_turn,
            outcome=loop.outcome,
        )
        if status.skip_final_save:
            yield await self._emit_recoverable_event(completed, session=session)
        else:
            yield await emit(completed)

    def _run_completed(
        self,
        run_stats: RunStats,
        *,
        session_id: str,
        turns: int,
        outcome: RunOutcome,
    ) -> AgentCompleted:
        """Build a run's terminal event with the mode, model, and effort it ended with."""
        return run_stats.completed(
            session_id=session_id,
            turns=turns,
            outcome=outcome,
            mode=self.mode,
            model=self.model or self.provider.default_model,
            effort=self.effort,
        )

    def _run_tools(
        self,
        registry: ToolRegistry | None,
        operation_tool_names: frozenset[str] | None,
        *,
        context: ToolContext,
        progress_sink: LiveEventSink | None = None,
        image_store: ImageStore | None = None,
    ) -> tuple[tuple[ToolSpec, ...], ConfiguredToolExecutor]:
        """Select the tools one run may use and build the executor that enforces them.

        Args:
            registry (ToolRegistry | None): The run's tool registry, if any.
            operation_tool_names (frozenset[str] | None): Names an operation
                restricts the run to, or None for every tool the mode allows.
            context (ToolContext): Tool context the executor runs tools with.
            progress_sink (LiveEventSink | None, optional): Where running tools'
                progress goes, or None when nobody listens. Defaults to None.
            image_store (ImageStore | None, optional): Stores the images a tool
                returns in the run's session before its result references them.
                Defaults to None, which drops them.

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
        policy = (
            operation_policy
            if operation_policy is not None
            else self._effective_tool_policy(effective_tools)
        )
        offered_tools, catalog = self._apply_mcp_exposure(effective_tools, registry)
        if catalog is not None:
            # Only the discovery tools are added. Built-ins keep exactly the policy they
            # had, and catalog tools are checked against it when `mcp_call` resolves.
            policy = replace(
                policy, allowed_tools=policy.allowed_tools | frozenset(DISCOVERY_TOOL_NAMES)
            )
        executor = ConfiguredToolExecutor(
            registry=registry,
            context=context,
            policy=policy,
            approval_policy=self.tool_approval_policy,
            progress_sink=progress_sink,
            mcp_catalog=catalog,
            image_store=image_store,
        )
        return offered_tools, executor

    def _apply_mcp_exposure(
        self, tools: tuple[ToolSpec, ...], registry: ToolRegistry | None
    ) -> tuple[tuple[ToolSpec, ...], McpCatalog | None]:
        """Replace MCP tools with the discovery tools when exposure is on demand.

        The catalog holds exactly the MCP tools among ``tools``: what direct mode
        would offer this run after selection, policy, plan mode and any operation
        limit. ``auto`` finds them on demand only when their schemas reach
        ``AUTO_ON_DEMAND_SCHEMA_TOKENS``, so the decision follows each run's own
        catalog. The discovery tools are registered in ``registry``, which must be an
        operation's own copy, so the executor and prompt find them.

        Args:
            tools (tuple[ToolSpec, ...]): The tools direct mode would offer.
            registry (ToolRegistry | None): The operation's registry copy.

        Returns:
            tuple[tuple[ToolSpec, ...], McpCatalog | None]: The tools to offer, and
            the catalog, or None when exposure is direct or no MCP tool is available.
        """

        if self.mcp_tool_exposure == "direct" or registry is None:
            return tools, None
        registered = set(registry.names())
        if registered.intersection(DISCOVERY_TOOL_NAMES):
            # Another tool already uses a discovery tool's name; never shadow it.
            return tools, None
        catalog = McpCatalog.from_tools(
            (registry.get(spec.name) for spec in tools if spec.name in registered),
            connected=self._mcp_server_connected,
        )
        if not catalog:
            return tools, None
        if self.mcp_tool_exposure == "auto" and (
            sum(tool_schema_tokens(spec) for spec in tools if catalog.get(spec.name) is not None)
            < AUTO_ON_DEMAND_SCHEMA_TOKENS
        ):
            return tools, None
        offered = [spec for spec in tools if catalog.get(spec.name) is None]
        for tool in discovery_tools(catalog):
            registry.register(
                tool,
                replace=False,
                execution=ToolExecutionMetadata(parallel_safe=False),
                prompt=DISCOVERY_PROMPT,
            )
            offered.append(ToolSpec.from_tool(tool))
        return tuple(offered), catalog

    def _progress_sink(self, live_event_sink: LiveEventSink | None) -> LiveEventSink | None:
        """Return where tool progress goes: extensions observing events, then the caller.

        Progress is transient, so unlike the run's other events it is not persisted.
        """

        bus = self.events
        if bus is None and live_event_sink is None:
            return None

        async def publish(event: WispEvent) -> None:
            if bus is not None:
                await bus.emit(event)
            if live_event_sink is not None:
                await live_event_sink(event)

        return publish

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
                tool_result_image_loader=partial(self._load_tool_result_images, session),
            ),
            messages=self._conversation_history(history),
        )
        retained = self._retained_queues.pop(session.session_id, None)
        if retained is None:
            return harness, False
        harness.set_steering_mode(retained.steering_mode)
        harness.set_follow_up_mode(retained.follow_up_mode)
        for message in retained.messages.steering:
            harness.steer_message(self._restored_queue_message(message))
        for message in retained.messages.follow_up:
            harness.follow_up_message(self._restored_queue_message(message))
        return harness, True

    def _restored_queue_message(self, message: Message) -> Message:
        """Fit a message queued by an earlier run into this run's image budget.

        The message kept its image bytes from that run, but the model may have
        changed since to one that cannot take images, and this run's history may
        leave less room. Images that no longer fit go as placeholder text.
        """

        message = _for_image_support(message, takes_images=self._model_takes_images())
        image_size = _queued_image_bytes((message,))
        if image_size > self._tool_image_allowance:
            return message.model_copy(update={"image_bytes": None})
        self._tool_image_allowance -= image_size
        return message

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
                images=event.images or None,
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
            # Stats count what a run would send, so they apply the run's MCP exposure.
            stats_registry = self._operation_tool_registry()
            stats_tools, stats_catalog = self._apply_mcp_exposure(
                self._effective_tools(stats_registry), stats_registry
            )
            prompt = await self._prompt_messages_async(
                stats_tools,
                registry=stats_registry,
                session_id=session.session_id if session is not None else None,
                session=session,
                mcp_catalog=stats_catalog,
            )
            provider_messages = (*prompt.messages, *self._normalize_provider_messages(history))
            observation = self._context_observations.get(
                session.session_id if session is not None else ""
            )
            observed_tokens = observation.input_tokens if observation is not None else None
            return build_session_stats(
                session_id=session.session_id if session is not None else None,
                entries=entries,
                replay=replay,
                provider_messages=provider_messages,
                tools=stats_tools,
                context_window=self._context_window(),
                reserve_tokens=self._effective_context_reserve_tokens(),
                provider=self.provider.name,
                model=self.model or self.provider.default_model,
                observed_tokens=observed_tokens,
                auto_compaction_enabled=self.auto_compaction_enabled,
                prompt_parts=prompt.parts,
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
            # The discovery tools exist only in a run's own registry, never in the
            # session's: they read the catalog, and `mcp_call` never runs anything
            # itself (a proxied call that ran a tool is judged by its `target`). A
            # registered tool that shares their name is judged above by its safety.
            return name not in DISCOVERY_TOOL_NAMES

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
        mcp_catalog: McpCatalog | None = None,
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
        additional_guidance = (
            PromptPart("skills", "skill index", skill_index),
            PromptPart("project_context", "web search", self._web_search_guidance()),
            PromptPart(
                "mcp_tools",
                "mcp servers",
                catalog_guidance(mcp_catalog) if mcp_catalog is not None else "",
            ),
        )
        if self.prompt_messages is not None:
            custom = assemble_custom_prompt(
                self.prompt_messages,
                additional_guidance=additional_guidance,
                mode=self.mode,
            )
            return _SessionPrompt(messages=custom.messages, parts=custom.parts)
        repository_status = (
            self._repository_status_snapshot(session_id, operation_context.cwd, session=session)
            if self.trusted and session_id is not None
            else None
        )
        prompt = assemble_prompt(
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
            messages=prompt.messages,
            parts=prompt.parts,
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
        mcp_catalog: McpCatalog | None = None,
    ) -> _SessionPrompt:
        """Build the operation prompt without blocking the event-loop thread."""

        return await anyio.to_thread.run_sync(
            lambda: self._prompt_messages(
                tools,
                registry=registry,
                context=context,
                session_id=session_id,
                session=session,
                mcp_catalog=mcp_catalog,
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
        tool_cwd: Path | None = None,
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
                duration_ms=event.duration_ms,
                process_started_at=event.process_started_at,
                process_ended_at=event.process_ended_at,
                # `/diff` attributes files to these calls by the folder they ran in.
                cwd=(
                    str(tool_cwd.resolve(strict=False))
                    if tool_cwd is not None and event.name in ("edit", "write")
                    else None
                ),
                target=event.target,
                edit_locations=event.edit_locations or None,
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
            active_history = await self._owner._with_image_bytes(
                self._session,
                await anyio.to_thread.run_sync(self._session.read_context_messages),
            )
            self._owner._start_tool_image_allowance(
                active_history, reserved_bytes=self._pending_queue_image_bytes()
            )
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

    def _pending_queue_image_bytes(self) -> int:
        """Return the image bytes of messages still queued for this run's requests."""

        queued = self._harness.queued_messages
        return _queued_image_bytes((*queued.steering, *queued.follow_up))

    async def _replacement_after_compaction(
        self,
        continuation_messages: Sequence[Message],
        *,
        has_native_continuation: bool,
        injected_messages: Sequence[Message] = (),
    ) -> RequestBoundaryDecision:
        """Rehydrate durable context into the least disruptive loop transition."""

        owner = self._owner
        active_history = await owner._with_image_bytes(
            self._session,
            await anyio.to_thread.run_sync(self._session.read_context_messages),
        )
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
        # Only a fresh replacement drops the provider's replay of earlier tool results
        # and their images; a rebase above keeps it, and with it the allowance spent.
        owner._start_tool_image_allowance(
            active_history, reserved_bytes=self._pending_queue_image_bytes()
        )
        return RequestBoundaryDecision(
            messages=owner._normalize_provider_messages(
                replacement,
                active_from=replacement_active_from,
            )
        )


def _run_setup(tools: Sequence[ToolSpec], instructions: Sequence[Message]) -> RunSetup:
    """Fingerprint the tools and system messages a run offers the model.

    Args:
        tools (Sequence[ToolSpec]): The tools offered to the model, in offered order.
        instructions (Sequence[Message]): The system messages the run starts with.

    Returns:
        RunSetup: The tool names and SHA-256 hashes of the canonical tool specs
        and of the system messages. Offered order is part of the tools hash
        because it changes the prompt.
    """

    tool_specs = [
        {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
        for tool in tools
    ]
    system_messages = [message.content for message in instructions if message.role == "system"]
    return RunSetup(
        tool_names=tuple(tool.name for tool in tools),
        tools_sha256=_canonical_sha256(tool_specs),
        instructions_sha256=_canonical_sha256(system_messages),
    )


def _canonical_sha256(value: object) -> str:
    """Hash a JSON value so equal content always gives the same digest."""

    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
