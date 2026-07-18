"""Typed command models for Wisp JSONL RPC clients."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

type ApprovalScope = Literal["once", "tool_session", "all_session"]


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


class CompactCommand(RpcCommandModel):
    """Compact the active persisted session context."""

    type: Literal["compact"] = "compact"
    instructions: str | None = None


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
    """Update RPC process configuration for future prompt commands."""

    type: Literal["configure"] = "configure"
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
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
    | CompactCommand
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
