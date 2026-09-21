"""Persisted-session, transcript page, session-tree, and message-queue events."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from wisp.events._base import (
    FinishReason,
    JsonObject,
    MessageRole,
    QueueKind,
    QueueMode,
    ToolPresentationStatus,
    WispEvent,
    _MessageOriginEvent,
)
from wisp.events.usage import TokenUsage, UsageCost
from wisp.skills.models import SkillInvocationEvidence


class RpcMessageToolCallSnapshot(BaseModel):
    """Bounded tool-call state retained on an RPC transcript message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str
    name: str
    arguments: JsonObject
    arguments_original_bytes: int = Field(ge=0)
    arguments_truncated: bool = False
    parse_error: str | None = None


class RpcMessageToolResultSnapshot(BaseModel):
    """Bounded presentation metadata for a persisted tool-result message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: ToolPresentationStatus | None = None
    exit_code: int | None = None
    output_has_exit_status: bool = False
    before_text: str | None = None
    created: bool = False
    summary: str | None = None
    truncated: bool = False


class RpcSkillInvocationSnapshot(BaseModel):
    """Bounded explicit-skill evidence attached to an RPC transcript message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    original_content: str
    original_content_bytes: int = Field(ge=0)
    original_content_truncated: bool = False
    request: str
    request_bytes: int = Field(ge=0)
    request_truncated: bool = False
    content_sha256: str
    instructions_truncated: bool = False


class RpcMessageSnapshot(BaseModel):
    """One bounded, frontend-oriented persisted message snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_id: str
    parent_id: str | None = None
    operation_id: str | None = None
    created_at: datetime
    role: MessageRole
    content: str
    content_original_bytes: int = Field(ge=0)
    content_truncated: bool = False
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_calls: tuple[RpcMessageToolCallSnapshot, ...] = ()
    tool_calls_original_count: int = Field(default=0, ge=0)
    tool_calls_truncated: bool = False
    response_id: str | None = None
    finish_reason: FinishReason | None = None
    is_error: bool | None = None
    usage: TokenUsage | None = None
    cost: UsageCost | None = None
    tool_result: RpcMessageToolResultSnapshot | None = None
    skill_invocation: RpcSkillInvocationSnapshot | None = None

    @model_validator(mode="after")
    def _validate_tool_result_role(self) -> Self:
        if self.tool_result is not None and self.role != "tool":
            raise ValueError("RPC tool-result metadata is valid only on tool messages")
        return self


class RpcSessionSummary(BaseModel):
    """One persisted session summary returned by the RPC session catalog."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1)
    session_path: Path
    updated_at: datetime
    entry_count: int = Field(ge=0)
    active_leaf_id: str | None = Field(default=None, min_length=1)
    name: str | None = None


class RpcSessionTreeNode(BaseModel):
    """One bounded node summary from a persisted session tree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_id: str = Field(min_length=1)
    parent_id: str | None = Field(default=None, min_length=1)
    operation_id: str | None = None
    created_at: datetime
    kind: Literal["message", "event", "compaction"]
    role: MessageRole | None = None
    preview: str
    preview_truncated: bool = False

    @model_validator(mode="after")
    def _validate_role(self) -> Self:
        if (self.kind == "message") != (self.role is not None):
            raise ValueError("RPC session tree message nodes must include role only for messages")
        return self


class SessionSaved(WispEvent):
    type: Literal["session.saved"] = "session.saved"
    session_id: str
    path: Path


class RpcMessagesReported(WispEvent):
    """On-demand, bounded persisted transcript page returned over RPC."""

    type: Literal["rpc.messages"] = "rpc.messages"
    command_id: str
    session_id: str | None = None
    session_path: Path | None = None
    active_leaf_id: str | None = None
    messages: tuple[RpcMessageSnapshot, ...] = ()
    truncated: bool = False
    next_before_entry_id: str | None = None
    next_after_entry_id: str | None = None

    @model_validator(mode="after")
    def _validate_cursors(self) -> Self:
        if (
            self.next_before_entry_id is not None or self.next_after_entry_id is not None
        ) and not self.truncated:
            raise ValueError("RPC message reports cannot include a cursor unless truncated")
        if self.next_before_entry_id is not None and self.next_after_entry_id is not None:
            raise ValueError("RPC message report cursors are mutually exclusive")
        return self


class RpcSessionsReported(WispEvent):
    """On-demand, bounded persisted session catalog returned over RPC."""

    type: Literal["rpc.sessions"] = "rpc.sessions"
    command_id: str
    sessions: tuple[RpcSessionSummary, ...] = ()
    selected_session_id: str | None = Field(default=None, min_length=1)
    selected_session_path: Path | None = None
    selected_session_name: str | None = None

    @model_validator(mode="after")
    def _validate_selection(self) -> Self:
        if (self.selected_session_id is None) != (self.selected_session_path is None):
            raise ValueError(
                "RPC session reports must include selected_session_id and "
                "selected_session_path together"
            )
        return self


class RpcSessionSelected(WispEvent):
    """Confirmation that an RPC session selection has become active."""

    type: Literal["rpc.session.selected"] = "rpc.session.selected"
    command_id: str
    session_id: str = Field(min_length=1)
    session_path: Path
    active_leaf_id: str | None = Field(default=None, min_length=1)
    entry_count: int = Field(ge=0)
    session_name: str | None = None


class _RpcSessionDerived(WispEvent):
    """Shared source and target identity for RPC session derivation."""

    command_id: str
    source_session_id: str = Field(min_length=1)
    source_session_path: Path
    source_active_leaf_id: str | None = Field(default=None, min_length=1)
    source_session_name: str | None = None
    session_id: str = Field(min_length=1)
    session_path: Path
    active_leaf_id: str | None = Field(default=None, min_length=1)
    session_name: str | None = None
    entry_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_derivation(self) -> Self:
        if self.source_session_id == self.session_id:
            raise ValueError("RPC session derivation must create a new session id")
        return self


class RpcSessionCloned(_RpcSessionDerived):
    """Confirmation that the active path was cloned and selected."""

    type: Literal["rpc.session.cloned"] = "rpc.session.cloned"

    @model_validator(mode="after")
    def _validate_clone(self) -> Self:
        if self.active_leaf_id is None:
            raise ValueError("RPC cloned sessions require an active leaf")
        if self.entry_count == 0:
            raise ValueError("RPC cloned sessions require at least one entry")
        return self


class RpcSessionForked(_RpcSessionDerived):
    """Confirmation that a session was forked before a user message and selected."""

    type: Literal["rpc.session.forked"] = "rpc.session.forked"
    selected_entry_id: str = Field(min_length=1)
    selected_prompt: str


class RpcSessionNameChanged(WispEvent):
    """Confirmation that a session display name metadata record was appended."""

    type: Literal["rpc.session.name_changed"] = "rpc.session.name_changed"
    command_id: str
    session_id: str = Field(min_length=1)
    session_path: Path
    previous_name: str | None = None
    name: str | None = None
    entry_count: int = Field(ge=0)


class RpcSessionTreeReported(WispEvent):
    """Bounded append-order page of the selected persisted session tree."""

    type: Literal["rpc.session.tree"] = "rpc.session.tree"
    command_id: str
    session_id: str | None = Field(default=None, min_length=1)
    session_path: Path | None = None
    active_leaf_id: str | None = Field(default=None, min_length=1)
    total_node_count: int = Field(ge=0)
    nodes: tuple[RpcSessionTreeNode, ...] = ()
    truncated: bool = False
    next_after_entry_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate_tree_report(self) -> Self:
        if (self.session_id is None) != (self.session_path is None):
            raise ValueError(
                "RPC session tree reports must include session_id and session_path together"
            )
        if len(self.nodes) > self.total_node_count:
            raise ValueError("RPC session tree page cannot exceed total_node_count")
        if (self.next_after_entry_id is not None) != self.truncated:
            raise ValueError(
                "RPC session tree reports require next_after_entry_id exactly when truncated"
            )
        if self.truncated and (
            not self.nodes or self.next_after_entry_id != self.nodes[-1].entry_id
        ):
            raise ValueError(
                "RPC session tree next_after_entry_id must identify the final returned node"
            )
        if len({node.entry_id for node in self.nodes}) != len(self.nodes):
            raise ValueError("RPC session tree pages cannot contain duplicate entry ids")
        if self.session_id is None and (
            self.active_leaf_id is not None or self.total_node_count or self.nodes or self.truncated
        ):
            raise ValueError("RPC session tree reports without a session must be empty")
        return self


class RpcSessionTreeNavigated(WispEvent):
    """Confirmation that a selected session's active path changed."""

    type: Literal["rpc.session.tree.navigated"] = "rpc.session.tree.navigated"
    command_id: str
    session_id: str = Field(min_length=1)
    session_path: Path
    selected_entry_id: str = Field(min_length=1)
    previous_active_leaf_id: str | None = Field(default=None, min_length=1)
    active_leaf_id: str | None = Field(default=None, min_length=1)
    editor_text: str | None = None
    changed: bool
    entry_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_transition(self) -> Self:
        if self.changed == (self.previous_active_leaf_id == self.active_leaf_id):
            raise ValueError(
                "RPC session tree navigation changed must match the active-leaf transition"
            )
        return self


class RpcSessionTreeUnreverted(WispEvent):
    """Confirmation that the latest explicit tree navigation was reversed."""

    type: Literal["rpc.session.tree.unreverted"] = "rpc.session.tree.unreverted"
    command_id: str
    session_id: str = Field(min_length=1)
    session_path: Path
    source_transition_id: str = Field(min_length=1)
    previous_active_leaf_id: str | None = Field(default=None, min_length=1)
    active_leaf_id: str | None = Field(default=None, min_length=1)
    entry_count: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_unrevert(self) -> Self:
        if self.previous_active_leaf_id == self.active_leaf_id:
            raise ValueError("RPC session tree unrevert must change the active leaf")
        return self


class QueueUpdated(WispEvent):
    """Current harness-owned steering and follow-up queue state."""

    type: Literal["queue.updated"] = "queue.updated"
    steering: tuple[str, ...] = ()
    follow_up: tuple[str, ...] = ()
    steering_mode: QueueMode = "one_at_a_time"
    follow_up_mode: QueueMode = "one_at_a_time"
    token: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Opaque active-queue revision; absent for retained or legacy snapshots.",
    )
    command_id: str | None = Field(
        default=None,
        description="Originating RPC command for a solicited snapshot; absent for run events.",
    )


class QueueItemsRemoved(WispEvent):
    """Queued text removed by an RPC pop or clear operation."""

    type: Literal["queue.items.removed"] = "queue.items.removed"
    command_id: str
    operation: Literal["pop", "clear"]
    kind: QueueKind | None = None
    steering: tuple[str, ...] = ()
    follow_up: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_removal(self) -> Self:
        if self.operation == "pop" and self.kind is None:
            raise ValueError("queue pop results require a queue kind")
        if self.kind == "steering" and self.follow_up:
            raise ValueError("steering queue removal results cannot contain follow-up items")
        if self.kind == "follow_up" and self.steering:
            raise ValueError("follow-up queue removal results cannot contain steering items")
        if self.operation == "pop" and len(self.steering) + len(self.follow_up) > 1:
            raise ValueError("queue pop results can contain at most one removed item")
        return self


class QueueMessageInjected(_MessageOriginEvent):
    """A queued user message crossed into the active transcript."""

    type: Literal["queue.message.injected"] = "queue.message.injected"
    kind: QueueKind
    content: str
    skill_invocation: SkillInvocationEvidence | None = None


class SkillInvoked(WispEvent):
    """An explicit skill directive became one provider-visible user message."""

    type: Literal["skill.invoked"] = "skill.invoked"
    session_id: str
    message_entry_id: str
    invocation: SkillInvocationEvidence
    provider_content: str
    queue_kind: QueueKind | None = None
