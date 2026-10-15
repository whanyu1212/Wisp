"""Tests for proposing MCP server changes that the user must confirm."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

import anyio
import pytest

from tests.support.cli import DangerTool
from tests.support.paths import FIXTURES_DIR
from wisp.config.mcp_servers import add_user_mcp_servers, load_user_mcp_servers
from wisp.config.runtime import WispConfig
from wisp.events import (
    RpcCommandFinished,
    RpcMcpStatusReported,
    ToolApprovalRequested,
    ToolResultReady,
)
from wisp.mcp.config import McpServerConfig
from wisp.providers.base import ToolCall, ToolCallResult, ToolSpec
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderToolCallCompleted,
)
from wisp.rpc.host import RpcToolApprovalPolicy
from wisp.sdk import InProcessOptions, InProcessWisp
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.context import ToolContext
from wisp.tools.mcp_servers import (
    ListMcpServersTool,
    ProposeMcpServerChangeTool,
    ProposeMcpServerTool,
    proposed_server,
)

SECRET = "sentinel-secret-value-1234567890abcdef"


def _proposal(**extra: object) -> dict[str, object]:
    return {"name": "github", "command": "npx", "args": ["-y", "server-github"], **extra}


@pytest.mark.parametrize(
    "args",
    [
        ["-y", "server", "--token=abc"],
        ["-y", "server", "--api-key", "abc"],
        ["-y", "server", "GITHUB_TOKEN=abc"],
        ["-y", "server", "ghp_0123456789abcdef0123456789abcdef0123"],
        ["-y", "server", SECRET],
        ["-y", "server-postgres", "postgresql://admin:hunter2pass@db.example.com/prod"],
        ["mcp-remote", "https://e.com/mcp", "--header", "Authorization: Bearer abcdef123456"],
        ["-y", "server", "--key", "3f9a"],
        ["MY_PAT=abc123", "npx", "server"],
    ],
)
def test_a_proposal_that_seems_to_carry_a_secret_is_rejected(args: list[str]) -> None:
    problem = ProposeMcpServerTool().check_arguments(_proposal(args=args))

    assert problem is not None
    assert "looks like a secret" in problem
    assert all(arg not in problem for arg in args[2:])


@pytest.mark.parametrize(
    "arguments",
    [
        _proposal(),
        _proposal(args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp/project"]),
        _proposal(args=["--port=8080", "--log-level", "debug"], env_from=["GITHUB_TOKEN"]),
        {"name": "docs", "url": "https://developers.openai.com/mcp"},
        _proposal(args=["--project", "123e4567-e89b-12d3-a456-426614174000"]),
        _proposal(args=["--token-file", "/home/me/.config/token"]),
        _proposal(args=["--pass-through", "stdio"]),
        _proposal(
            args=[
                "-y",
                "mcp-remote",
                "https://e.com/mcp",
                "--header",
                "Authorization: Bearer ${AUTH}",
            ],
            env_from=["AUTH"],
        ),
        _proposal(
            command="docker",
            args=["run", "-i", "--rm", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN", "ghcr.io/github/mcp"],
        ),
    ],
)
def test_ordinary_proposals_pass_the_check(arguments: dict[str, object]) -> None:
    assert ProposeMcpServerTool().check_arguments(arguments) is None


def test_invalid_proposals_are_rejected_before_the_user_is_asked() -> None:
    tool = ProposeMcpServerTool()
    assert "invalid" in (tool.check_arguments({"name": "Bad Name", "command": "x"}) or "")
    assert tool.check_arguments({"name": "x", "command": "a", "url": "https://e.com"})
    assert tool.check_arguments(_proposal(replace="yes")) == "replace must be a boolean"


def test_a_change_is_checked_without_reading_settings() -> None:
    tool = ProposeMcpServerChangeTool()
    # A server that is not saved is reported when the user confirms.
    assert tool.check_arguments({"name": "docs", "action": "remove"}) is None
    assert tool.check_arguments({"name": "docs", "action": "erase"})
    assert tool.check_arguments({"name": "", "action": "remove"})


def test_listing_never_shows_environment_values() -> None:
    add_user_mcp_servers((McpServerConfig(name="github", command="npx", env={"TOKEN": SECRET}),))

    result = anyio.run(ListMcpServersTool().run, {}, ToolContext(cwd=Path.cwd()))

    assert "github (enabled): runs npx" in result.text
    assert "TOKEN" in result.text
    assert SECRET not in result.text


def test_no_grant_or_flag_approves_a_proposal() -> None:
    tool = ProposeMcpServerTool()
    for policy in (
        ToolApprovalPolicy.approve_all(),
        ToolApprovalPolicy.approve_tool_names({tool.name}),
    ):
        assert policy.requires_approval(tool)
        assert not policy.approves(tool)
        assert "--yes and saved permissions never approve it" in policy.block_reason(tool)


def _rpc_policy(
    applied: list[dict[str, object]], error: str | None = None
) -> RpcToolApprovalPolicy:
    policy = RpcToolApprovalPolicy(ToolApprovalPolicy.approve_all())

    def confirm(tool_name: str, arguments: Mapping[str, object]) -> str | None:
        del tool_name
        applied.append(dict(arguments))
        return error

    policy.register_confirmation(ProposeMcpServerTool.name, confirm)
    return policy


def test_yolo_and_session_scopes_still_need_a_one_time_confirmation() -> None:
    applied: list[dict[str, object]] = []
    policy = _rpc_policy(applied)
    tool = ProposeMcpServerTool()
    arguments = _proposal()

    policy.prepare_approval(tool, call_id="call-1", arguments=arguments)
    assert policy.has_pending_approval(call_id="call-1")
    assert policy.resolve_approval(call_id="call-1", approved=True, scope="all_session")
    # Applied off the event loop, by the call that waits for the decision.
    decision = anyio.run(lambda: policy.await_approval(tool, call_id="call-1", arguments={}))
    assert decision.approved
    assert applied == [arguments]
    # The answer was a one-time confirmation: nothing was granted.
    assert tool.name not in policy.approved_tools
    policy.prepare_approval(tool, call_id="call-2", arguments=arguments)
    assert policy.has_pending_approval(call_id="call-2")


def test_another_tools_grant_never_releases_a_pending_proposal() -> None:
    applied: list[dict[str, object]] = []
    policy = _rpc_policy(applied)
    policy.approved_safety = frozenset()
    policy.prepare_approval(ProposeMcpServerTool(), call_id="proposal", arguments=_proposal())
    policy.prepare_approval(DangerTool(), call_id="danger", arguments={})
    policy.resolve_approval(call_id="danger", approved=True, scope="all_session")

    assert policy.has_pending_approval(call_id="proposal")
    assert applied == []


def test_a_proposal_that_cannot_be_applied_is_denied() -> None:
    applied: list[dict[str, object]] = []
    policy = _rpc_policy(applied, error="an MCP server named 'github' already exists")
    policy.prepare_approval(ProposeMcpServerTool(), call_id="call-1", arguments=_proposal())

    policy.resolve_approval(call_id="call-1", approved=True)

    decision = anyio.run(
        lambda: policy.await_approval(ProposeMcpServerTool(), call_id="call-1", arguments={})
    )
    assert not decision.approved
    assert decision.reason == "an MCP server named 'github' already exists"

    unhandled = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval())
    unhandled.prepare_approval(ProposeMcpServerTool(), call_id="call-2", arguments=_proposal())
    unhandled.resolve_approval(call_id="call-2", approved=True)
    decision = anyio.run(
        lambda: unhandled.await_approval(ProposeMcpServerTool(), call_id="call-2", arguments={})
    )
    assert not decision.approved
    assert "cannot apply" in (decision.reason or "")


class _ProposingProvider:
    """Proposes the fixture server once, then answers in text."""

    name = "proposer"
    default_model: str | None = "proposer"

    def __init__(self, arguments: dict[str, object]) -> None:
        self._arguments = arguments

    async def stream(
        self,
        messages: Sequence[object],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        del messages, tools, previous_response_id, effort
        yield ProviderResponseStarted(model=model or "proposer")
        if not tool_results:
            call = ToolCall(
                call_id="call-1",
                name="propose_mcp_server",
                arguments=self._arguments,
                response_id="response-1",
            )
            yield ProviderToolCallCompleted(tool_call=call)
            yield ProviderResponseCompleted(
                content="", tool_calls=(call,), response_id="response-1", finish_reason="tool_calls"
            )
            return
        yield ProviderTextDelta(delta="done")
        yield ProviderResponseCompleted(content="done", response_id="response-2")


@pytest.mark.process
def test_a_confirmed_proposal_is_saved_and_connected_after_the_reply(tmp_path: Path) -> None:
    arguments: dict[str, object] = {
        "name": "fixture",
        "command": sys.executable,
        "args": ["-u", str(FIXTURES_DIR / "mcp_stdio_server.py")],
    }

    async def scenario() -> tuple[list[object], str]:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path / "sessions"),
            options=InProcessOptions(startup_trusted=True, all_tools=True),
        )
        host = controller._in_process_transport._host
        host.agent.provider = _ProposingProvider(arguments)
        events: list[object] = []
        status_id = ""
        try:
            prompt_id = await controller.prompt("add the fixture server", command_id="prompt-1")
            async for event in controller.events():
                events.append(event)
                if isinstance(event, ToolApprovalRequested):
                    await controller.approve(event.call_id)
                elif isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                    status_id = await controller.get_mcp_status(command_id="status-1")
                elif isinstance(event, RpcCommandFinished) and event.command_id == status_id:
                    break
        finally:
            await controller.aclose()
        return events, status_id

    events, status_id = anyio.run(scenario)

    request = next(event for event in events if isinstance(event, ToolApprovalRequested))
    assert request.grants_allowed is False
    result = next(event for event in events if isinstance(event, ToolResultReady))
    assert result.is_error is False
    assert "available from the user's next message" in result.output
    status = next(
        event
        for event in events
        if isinstance(event, RpcMcpStatusReported) and event.command_id == status_id
    )
    assert [(server.name, server.status) for server in status.status.servers] == [
        ("fixture", "connected")
    ]
    assert status.status.servers[0].tool_names
    assert [server.name for server in load_user_mcp_servers()] == ["fixture"]


def test_a_token_in_a_url_path_is_rejected() -> None:
    problem = ProposeMcpServerTool().check_arguments(
        {"name": "zap", "url": f"https://mcp.example.com/api/mcp/s/{SECRET}/mcp"}
    )
    assert problem is not None and "The URL looks like a secret" in problem
    assert SECRET not in problem


@pytest.mark.parametrize(
    "args",
    [["pkg\nWisp runs nothing else."], ["pkg\rx"], ["\x1b[31mred"], ["a\u202eb"], ["a\u200bb"]],
)
def test_control_and_invisible_characters_are_rejected(args: list[str]) -> None:
    problem = ProposeMcpServerTool().check_arguments(_proposal(args=args))
    assert problem is not None and "control characters" in problem


def test_listing_hides_arguments_and_url_paths() -> None:
    add_user_mcp_servers(
        (
            McpServerConfig(
                name="pg",
                command="/usr/local/bin/npx",
                args=("-y", "server-postgres", "postgresql://admin:pw@host/db"),
            ),
            McpServerConfig(name="zap", url=f"https://mcp.example.com/s/{SECRET}/mcp"),
        )
    )

    result = anyio.run(ListMcpServersTool().run, {}, ToolContext(cwd=Path.cwd()))

    assert "pg (enabled): runs npx with 3 arguments (hidden)" in result.text
    assert "zap (enabled): connects to https://mcp.example.com (path hidden)" in result.text
    assert "admin:pw" not in result.text and SECRET not in result.text


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (ProposeMcpServerTool(), _proposal(env={"TOKEN": SECRET})),
        (ProposeMcpServerTool(), _proposal(tool_safety={"x": "read"})),
        (ProposeMcpServerChangeTool(), {"name": "docs", "action": "remove", "force": True}),
    ],
)
def test_fields_a_proposal_would_not_show_are_rejected(
    tool: ProposeMcpServerTool | ProposeMcpServerChangeTool, arguments: dict[str, object]
) -> None:
    problem = tool.check_arguments(arguments)
    assert problem is not None and "Unsupported fields" in problem
    assert SECRET not in problem


def _http_proposal(**extra: object) -> dict[str, object]:
    return {"name": "linear", "url": "https://mcp.linear.app/mcp", **extra}


def test_an_http_proposal_can_forward_a_token_variable_into_a_header() -> None:
    arguments = _http_proposal(headers_from={"Authorization": "Bearer ${LINEAR_API_KEY}"})

    assert ProposeMcpServerTool().check_arguments(arguments) is None
    assert proposed_server(arguments).headers_from == (
        ("Authorization", "Bearer ${LINEAR_API_KEY}"),
    )


@pytest.mark.parametrize(
    "headers_from",
    [
        {"Authorization": f"Bearer {SECRET}"},
        {"Authorization": f"Bearer {SECRET} ${{TOKEN}}"},
        {"Authorization‮": "${TOKEN}"},
        {"Host": "${TOKEN}"},
        {"ghp_0123456789abcdef0123456789abcdef0123": "${HOME}"},
        ["Authorization"],
    ],
)
def test_a_header_proposal_that_could_carry_or_hide_a_value_is_rejected(
    headers_from: object,
) -> None:
    problem = ProposeMcpServerTool().check_arguments(_http_proposal(headers_from=headers_from))

    assert problem is not None
    assert SECRET not in problem


def test_a_literal_header_value_is_never_accepted_from_a_proposal() -> None:
    problem = ProposeMcpServerTool().check_arguments(
        _http_proposal(headers={"Authorization": f"Bearer {SECRET}"})
    )

    assert problem is not None and "Unsupported fields: headers" in problem
    assert SECRET not in problem


def test_listing_names_the_variable_a_header_sends() -> None:
    add_user_mcp_servers(
        (
            McpServerConfig.model_validate(
                {
                    "name": "linear",
                    "url": "https://mcp.linear.app/mcp",
                    "headers": {"X-Workspace": SECRET},
                    "headers_from": {"Authorization": "Bearer ${LINEAR_API_KEY}"},
                }
            ),
        )
    )

    result = anyio.run(ListMcpServersTool().run, {}, ToolContext(cwd=Path.cwd()))

    assert (
        "linear (enabled): connects to https://mcp.linear.app (path hidden); "
        "sends $LINEAR_API_KEY in header Authorization"
    ) in result.text
    assert SECRET not in result.text
