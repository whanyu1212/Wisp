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


class CancelCommand(RpcCommandModel):
    """Cancel a running prompt command."""

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


type RpcCommand = Annotated[
    PromptCommand
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
