"""Command parsing, dispatch, and resolver stubs shared by the RPC executor tests."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from tests.support.rpc import RpcExecutorFixture
from wisp.coding import CodingSession
from wisp.providers.base import ToolSpec
from wisp.rpc.commands import (
    ParsedRpcCommand,
    RpcCommandAdapter,
)
from wisp.rpc.coordinator import (
    _RpcDispatchResult,
    _RpcRunningCommand,
)
from wisp.rpc.execution import RpcCommandExecutor
from wisp.tools.context import ToolContext


class ApprovalResolver:
    def has_pending_approval(self, **_kwargs: object) -> bool:
        return False

    def resolve_approval(self, **_kwargs: object) -> bool:
        return False


class TrustResolver:
    async def resolve(self) -> bool:
        return True

    def resolve_request(self, **_kwargs: object) -> bool:
        return False


def parsed_command(payload: dict[str, object]) -> ParsedRpcCommand:
    try:
        command = RpcCommandAdapter.validate_json(json.dumps(payload))
    except ValidationError as exc:
        if isinstance(payload.get("type"), str) and any(
            error["type"] == "union_tag_invalid" for error in exc.errors(include_input=False)
        ):
            return ParsedRpcCommand.from_unknown(payload)
        raise
    return ParsedRpcCommand.from_known(command, payload=payload)


async def dispatch_parsed(
    executor: RpcCommandExecutor,
    payload: dict[str, object],
    running: _RpcRunningCommand | None = None,
) -> _RpcDispatchResult:
    return await executor.dispatch_parsed(parsed_command(payload), running)


def enable_project_init(fixture: RpcExecutorFixture, project_root: Path) -> None:
    fixture.agent = CodingSession(
        provider=fixture.runtime.providers.get("fake"),
        sessions=fixture.sessions,
        tools=(ToolSpec.from_tool(fixture.runtime.tools.get("write")),),
        tool_context=ToolContext(cwd=project_root),
        project_context_root=project_root,
    )
