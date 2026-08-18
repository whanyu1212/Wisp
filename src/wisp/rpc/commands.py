"""Typed command models for Wisp JSONL RPC clients."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from wisp.agent.mode import AgentMode
from wisp.events import QueueKind, QueueMode

type ApprovalScope = Literal["once", "tool_session", "all_session"]

QUEUE_RPC_COMMAND_TYPES = frozenset(
    {
        "clear_queue",
        "follow_up",
        "get_queue_state",
        "pop_queue",
        "set_queue_mode",
        "steer",
    }
)


class RpcCommandModel(BaseModel):
    """Base class for RPC commands sent to `wisp --mode rpc`."""

    model_config = ConfigDict(frozen=True)

    id: str | None = None

    def to_json_line(self) -> str:
        """Serialize this command as one JSONL command line."""

        return f"{self.model_dump_json(exclude_none=True)}\n"


class PromptCommand(RpcCommandModel):
    """Run one agent turn."""

    type: Literal["prompt"] = "prompt"
    prompt: str


class InitCommand(RpcCommandModel):
    """Inspect the active project and create its root AGENTS.md."""

    type: Literal["init"] = "init"


class CompactCommand(RpcCommandModel):
    """Compact the active persisted session context."""

    type: Literal["compact"] = "compact"
    instructions: str | None = None


class GetSessionStatsCommand(RpcCommandModel):
    """Return lifetime usage and current context-budget statistics."""

    type: Literal["get_session_stats"] = "get_session_stats"


class GetStateCommand(RpcCommandModel):
    """Return an immediate in-memory RPC state snapshot."""

    type: Literal["get_state"] = "get_state"


class GetCommandsCommand(RpcCommandModel):
    """Return an immediate in-memory command registry snapshot."""

    type: Literal["get_commands"] = "get_commands"


class GetSkillsCommand(RpcCommandModel):
    """Return the active immutable skill catalog snapshot."""

    type: Literal["get_skills"] = "get_skills"


class GetMcpStatusCommand(RpcCommandModel):
    """Return sanitized MCP server and registered-tool status."""

    type: Literal["get_mcp_status"] = "get_mcp_status"


class GetMessagesCommand(RpcCommandModel):
    """Return a bounded persisted transcript page."""

    type: Literal["get_messages"] = "get_messages"
    session_id: str | None = Field(default=None, min_length=1)
    limit: int = Field(default=200, ge=1, le=500, strict=True)
    before_entry_id: str | None = Field(default=None, min_length=1)


class GetSessionsCommand(RpcCommandModel):
    """Return a bounded catalog of persisted sessions."""

    type: Literal["get_sessions"] = "get_sessions"
    limit: int = Field(default=50, ge=0, le=200, strict=True)


class NewSessionCommand(RpcCommandModel):
    """Deselect the active session so the next prompt starts a fresh one."""

    type: Literal["new_session"] = "new_session"


class SelectSessionCommand(RpcCommandModel):
    """Select a persisted session as the active RPC session."""

    type: Literal["select_session"] = "select_session"
    session_id: str = Field(min_length=1)


class CloneSessionCommand(RpcCommandModel):
    """Clone the selected session's active path and select the clone."""

    type: Literal["clone_session"] = "clone_session"


class ForkSessionCommand(RpcCommandModel):
    """Fork before a persisted user message and select the fork."""

    type: Literal["fork_session"] = "fork_session"
    entry_id: str = Field(min_length=1)


class GetSessionTreeCommand(RpcCommandModel):
    """Return a bounded page of the selected session's tree."""

    type: Literal["get_session_tree"] = "get_session_tree"
    limit: int = Field(default=200, ge=1, le=500, strict=True)
    after_entry_id: str | None = Field(default=None, min_length=1)


class NavigateSessionTreeCommand(RpcCommandModel):
    """Navigate the selected session to one persisted tree entry."""

    type: Literal["navigate_session_tree"] = "navigate_session_tree"
    entry_id: str = Field(min_length=1)


class UnrevertSessionTreeCommand(RpcCommandModel):
    """Reverse the selected session's latest explicit tree navigation."""

    type: Literal["unrevert_session_tree"] = "unrevert_session_tree"


class SetSessionNameCommand(RpcCommandModel):
    """Set or clear one session's display name."""

    type: Literal["set_session_name"] = "set_session_name"
    name: str
    session_id: str | None = Field(default=None, min_length=1)


class SteerCommand(RpcCommandModel):
    """Queue text after the active run's current assistant/tool batch."""

    type: Literal["steer"] = "steer"
    content: str


class FollowUpCommand(RpcCommandModel):
    """Queue text for when the active run would otherwise stop."""

    type: Literal["follow_up"] = "follow_up"
    content: str


class GetQueueStateCommand(RpcCommandModel):
    """Return the active or retained queue state."""

    type: Literal["get_queue_state"] = "get_queue_state"


class SetQueueModeCommand(RpcCommandModel):
    """Set one active queue's drain mode."""

    type: Literal["set_queue_mode"] = "set_queue_mode"
    kind: QueueKind
    mode: QueueMode


class PopQueueCommand(RpcCommandModel):
    """Remove the latest item from one active queue."""

    type: Literal["pop_queue"] = "pop_queue"
    kind: QueueKind


class ClearQueueCommand(RpcCommandModel):
    """Clear one or both active queues."""

    type: Literal["clear_queue"] = "clear_queue"
    kind: QueueKind | None = None


class CancelCommand(RpcCommandModel):
    """Cancel a running prompt or compact command."""

    type: Literal["cancel"] = "cancel"
    target_id: str


class ApprovalCommand(RpcCommandModel):
    """Resolve a pending tool approval request."""

    type: Literal["approval"] = "approval"
    call_id: str
    approved: bool
    reason: str | None = None
    scope: ApprovalScope | None = None


class TrustCommand(RpcCommandModel):
    """Resolve a pending project-trust request."""

    type: Literal["trust"] = "trust"
    request_id: str
    trusted: bool
    reason: str | None = None
    transient: bool | None = None


class ShutdownCommand(RpcCommandModel):
    """Ask the RPC process to exit cleanly."""

    type: Literal["shutdown"] = "shutdown"


class ConfigureCommand(RpcCommandModel):
    """Update configuration in sequence before later RPC prompt commands."""

    type: Literal["configure"] = "configure"
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    auto_compaction_enabled: bool | None = None
    mode: AgentMode | None = None
    # `to_json_line()` uses `exclude_none=True`, so a bare `effort=None` is
    # indistinguishable on the wire from never having set `effort` at all --
    # the RPC server keys off "effort" in command (dict key presence), not
    # the value, to decide whether to touch agent.effort. This field is the
    # only way a client can explicitly request clearing effort back to the
    # provider's own default: it has no default value that `exclude_none`
    # would ever drop, since `False` is not `None`.
    clear_effort: bool = False


type RpcCommand = Annotated[
    PromptCommand
    | InitCommand
    | CompactCommand
    | GetSessionStatsCommand
    | GetStateCommand
    | GetCommandsCommand
    | GetSkillsCommand
    | GetMcpStatusCommand
    | GetMessagesCommand
    | GetSessionsCommand
    | NewSessionCommand
    | SelectSessionCommand
    | CloneSessionCommand
    | ForkSessionCommand
    | GetSessionTreeCommand
    | NavigateSessionTreeCommand
    | UnrevertSessionTreeCommand
    | SetSessionNameCommand
    | SteerCommand
    | FollowUpCommand
    | GetQueueStateCommand
    | SetQueueModeCommand
    | PopQueueCommand
    | ClearQueueCommand
    | CancelCommand
    | ApprovalCommand
    | TrustCommand
    | ShutdownCommand
    | ConfigureCommand,
    Field(discriminator="type"),
]
RpcCommandAdapter: TypeAdapter[RpcCommand] = TypeAdapter(RpcCommand)


def rpc_command_from_json(line: str) -> RpcCommand:
    """Parse one JSONL command line into a typed command model."""

    return RpcCommandAdapter.validate_json(line)
