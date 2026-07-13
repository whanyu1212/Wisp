"""Schema-v4 events emitted by the Wisp agent core."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

EVENT_SCHEMA_VERSION = 4
JsonObject = dict[str, object]
MessageRole = Literal["system", "user", "assistant", "tool"]
RunOutcome = Literal["completed", "failed", "cancelled"]
FinishReason = Literal["stop", "tool_calls", "length", "error", "cancelled"]
RetryReason = Literal["network", "timeout", "rate_limit", "server_error", "transient_http"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class WispEvent(BaseModel):
    """Base class for versioned events consumed by every Wisp frontend."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str
    schema_version: Literal[4] = 4
    timestamp: datetime = Field(default_factory=utc_now)


class ToolCallSnapshot(BaseModel):
    """Serializable tool-call state attached to a completed assistant message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str
    name: str
    arguments: JsonObject
    parse_error: str | None = None


class AgentStarted(WispEvent):
    type: Literal["agent.started"] = "agent.started"
    session_id: str


class TurnStarted(WispEvent):
    type: Literal["turn.started"] = "turn.started"
    turn: int


class ProviderRetrying(WispEvent):
    """A provider request failed before streaming and will be retried."""

    type: Literal["provider.retrying"] = "provider.retrying"
    turn: int
    provider: str
    attempt: int
    max_attempts: int
    delay_seconds: float
    reason: RetryReason
    status_code: int | None = None


class MessageStarted(WispEvent):
    type: Literal["message.started"] = "message.started"
    turn: int
    role: MessageRole = "assistant"


class MessageDelta(WispEvent):
    type: Literal["message.delta"] = "message.delta"
    turn: int
    delta: str
    role: MessageRole = "assistant"
    content_index: int = 0
    content_kind: Literal["text", "thinking"] = "text"


class MessageCompleted(WispEvent):
    type: Literal["message.completed"] = "message.completed"
    turn: int
    content: str
    finish_reason: FinishReason
    role: MessageRole = "assistant"
    response_id: str | None = None
    tool_calls: tuple[ToolCallSnapshot, ...] = ()


class ToolCallRequested(WispEvent):
    type: Literal["tool.call"] = "tool.call"
    call_id: str
    name: str
    arguments: JsonObject


class ToolExecutionStarted(WispEvent):
    type: Literal["tool.execution.started"] = "tool.execution.started"
    call_id: str
    name: str
    arguments: JsonObject


class ToolApprovalRequested(WispEvent):
    type: Literal["tool.approval.requested"] = "tool.approval.requested"
    call_id: str
    name: str
    arguments: JsonObject
    safety: Literal["read", "mutating", "command"]


class ToolApprovalResolved(WispEvent):
    type: Literal["tool.approval.resolved"] = "tool.approval.resolved"
    call_id: str
    name: str
    approved: bool
    reason: str | None = None


class TrustRequested(WispEvent):
    type: Literal["trust.requested"] = "trust.requested"
    request_id: str
    project_path: Path


class TrustResolved(WispEvent):
    type: Literal["trust.resolved"] = "trust.resolved"
    request_id: str
    project_path: Path
    trusted: bool
    reason: str | None = None


class ProjectConfigApplied(WispEvent):
    """A trusted project's local config was applied mid-session.

    Emitted by the RPC process after a first-run trust approval rebuilds the runtime
    from the project's ``.wisp/settings.json``. It lets an out-of-process front-end
    (the TUI) refresh the provider/model/auth it displays and mutates, so its header
    and ``/provider`` / ``/model`` / ``/auth`` / ``/login`` commands match the config
    the agent is actually running with.
    """

    type: Literal["project.config.applied"] = "project.config.applied"
    provider: str
    model: str | None = None
    auth_path: Path


class ToolExecutionEnded(WispEvent):
    type: Literal["tool.execution.ended"] = "tool.execution.ended"
    call_id: str
    name: str
    output: str
    is_error: bool
    # Process exit status for shell-like tools, promoted from ToolResult.data by
    # the executor (which knows the tool and holds the structured result). None
    # for tools without exit-code semantics and for error paths that produced no
    # ToolResult. See ToolResultReady.exit_code for why this is a narrow scalar
    # rather than the whole data mapping.
    exit_code: int | None = None
    # Pre-write file snapshot for the diff renderer, promoted from ToolResult.data
    # for write-like tools only. None for every other tool and for error paths.
    # See ToolResultReady.before_text for the wire/bounding rationale.
    before_text: str | None = None
    # Whether a write created a new file. Disambiguates before_text=None: a create
    # renders as pure additions, an overwrite with no usable snapshot falls back to
    # the summary. False for every non-write tool. See ToolResultReady.created.
    created: bool = False
    # One-line success summary for read-type tools (read/grep/find/ls), built from
    # the tool's structured data. None for tools without one. See
    # ToolResultReady.summary.
    summary: str | None = None


class ToolResultReady(WispEvent):
    type: Literal["tool.result"] = "tool.result"
    call_id: str
    name: str
    output: str
    is_error: bool
    # The one structured fact tool-aware rendering needs today: a shell command's
    # exit status. Deliberately a bounded, JSON-safe scalar rather than the raw
    # ToolResult.data mapping — the renderer runs in the TUI process and only sees
    # events *after* they cross the RPC wire (agent subprocess → JSON → client),
    # so the signal must serialize; but shipping the whole mapping would re-emit
    # unbounded tool data (e.g. an `ls` entry list) past ToolContext's bounds and
    # risk non-JSON payloads. This field crosses the only serialized consumer of
    # these events — the same-version RPC transport (sessions store Messages, not
    # raw events) — so no schema bump is needed. Set only for tools with genuine
    # exit-code semantics, so a card is never spuriously reddened.
    exit_code: int | None = None
    # The file's contents *before* a write overwrote them, so the renderer can show
    # a before/after diff instead of the flat "Wrote N bytes" summary. Like
    # exit_code, this is a bounded, JSON-safe scalar that must survive the RPC wire:
    # the write tool captures the prior text before clobbering the file, caps it
    # (dropping the snapshot entirely rather than shipping an unbounded or partial
    # file), and the executor promotes it here for the write tool only. None means
    # no snapshot — a newly created file, a binary/oversize/unreadable prior file,
    # or any non-write tool. The renderer uses ``created`` to tell those apart.
    before_text: str | None = None
    # Whether a write created a new file (vs. overwrote one). With before_text=None
    # this is the only thing separating a create — rendered as a pure-addition diff
    # of the new content — from an overwrite whose prior text couldn't be captured,
    # which must fall back to the plain summary rather than masquerade as a create.
    # A bounded JSON-safe scalar like before_text; False for every non-write tool.
    created: bool = False
    # A concise one-line summary of a successful read-type tool (read/grep/find/ls),
    # e.g. "read 42 lines from foo.py" or "grep: 3 matches", shown on the card in
    # place of a raw output dump. Like the other promoted fields it is a bounded,
    # JSON-safe scalar that must survive the RPC wire: the tool computes it from its
    # own structured data (never by re-parsing output), the summary module bounds it
    # at the source, and the executor promotes it only for tools that have one. None
    # for diff/shell tools, unknown tools, and error paths.
    summary: str | None = None


class TurnCompleted(WispEvent):
    type: Literal["turn.completed"] = "turn.completed"
    turn: int
    outcome: RunOutcome
    finish_reason: FinishReason


class SessionSaved(WispEvent):
    type: Literal["session.saved"] = "session.saved"
    session_id: str
    path: Path


class AgentCompleted(WispEvent):
    type: Literal["agent.completed"] = "agent.completed"
    session_id: str
    turns: int
    outcome: RunOutcome


class RpcCommandStarted(WispEvent):
    type: Literal["rpc.command.started"] = "rpc.command.started"
    command_id: str
    command_type: str


class RpcCommandFinished(WispEvent):
    type: Literal["rpc.command.finished"] = "rpc.command.finished"
    command_id: str
    command_type: str
    ok: bool
    error: str | None = None


class ErrorEvent(WispEvent):
    type: Literal["error"] = "error"
    message: str


type KnownWispEvent = Annotated[
    AgentStarted
    | TurnStarted
    | ProviderRetrying
    | MessageStarted
    | MessageDelta
    | MessageCompleted
    | ToolCallRequested
    | ToolExecutionStarted
    | ToolApprovalRequested
    | ToolApprovalResolved
    | TrustRequested
    | TrustResolved
    | ProjectConfigApplied
    | ToolExecutionEnded
    | ToolResultReady
    | TurnCompleted
    | SessionSaved
    | AgentCompleted
    | RpcCommandStarted
    | RpcCommandFinished
    | ErrorEvent,
    Field(discriminator="type"),
]
KnownWispEventAdapter: TypeAdapter[KnownWispEvent] = TypeAdapter(KnownWispEvent)
JsonObjectAdapter: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


def _require_current_schema(data: JsonObject) -> None:
    version = data.get("schema_version")
    if version != EVENT_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported Wisp event schema_version: {version!r}; expected {EVENT_SCHEMA_VERSION}"
        )


def wisp_event_from_json(line: str) -> KnownWispEvent:
    """Parse one current-schema JSONL event line into a typed Wisp event."""

    data = JsonObjectAdapter.validate_json(line)
    _require_current_schema(data)
    return KnownWispEventAdapter.validate_python(data)


def wisp_event_from_dict(data: JsonObject) -> KnownWispEvent:
    """Parse one current-schema event dictionary into a typed Wisp event."""

    _require_current_schema(data)
    return KnownWispEventAdapter.validate_python(data)
