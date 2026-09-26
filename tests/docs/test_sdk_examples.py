"""Executable coverage for the canonical Python SDK examples."""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from examples.sdk.control_requests import build_control_requests
from examples.sdk.minimal import prompt_once
from examples.sdk.persisted_sessions import persisted_session_workflow
from examples.sdk.safety_requests import (
    resolve_safety_request,
    run_with_safe_defaults,
)
from examples.sdk.subprocess_rpc import prompt_in_subprocess
from wisp.events import ToolApprovalRequested, TrustRequested
from wisp.rpc import ApprovalCommand, ApprovalScope, RpcCommand, TrustCommand


class RecordingSafetyController:
    """Record the public safety commands emitted by the example handler."""

    def __init__(self) -> None:
        self.commands: list[RpcCommand] = []

    async def trust(
        self,
        request_id: str,
        *,
        trusted: bool,
        reason: str | None = None,
        transient: bool = False,
        command_id: str | None = None,
    ) -> str:
        selected_id = command_id or "trust-1"
        self.commands.append(
            TrustCommand(
                id=selected_id,
                request_id=request_id,
                trusted=trusted,
                reason=reason,
                transient=True if transient else None,
            )
        )
        return selected_id

    async def approve(
        self,
        call_id: str,
        *,
        approved: bool = True,
        reason: str | None = None,
        scope: ApprovalScope | None = None,
        command_id: str | None = None,
    ) -> str:
        del scope
        selected_id = command_id or "approval-1"
        self.commands.append(
            ApprovalCommand(
                id=selected_id,
                call_id=call_id,
                approved=approved,
                reason=reason,
            )
        )
        return selected_id


def test_minimal_sdk_example_runs_offline(tmp_path: Path) -> None:
    response = anyio.run(prompt_once, tmp_path / "workspace", tmp_path / "sessions")

    assert response == "fake response to: hello from the SDK"


def test_safety_example_denies_project_trust_and_completes(tmp_path: Path) -> None:
    anyio.run(run_with_safe_defaults, tmp_path / "workspace", tmp_path / "sessions")


def test_safety_handler_denies_trust_and_tool_approval() -> None:
    controller = RecordingSafetyController()
    trust = TrustRequested(request_id="trust-request", project_path=Path("/workspace"))
    approval = ToolApprovalRequested(
        call_id="tool-call",
        name="bash",
        arguments={"command": "echo hello"},
        safety="command",
    )

    async def scenario() -> None:
        await resolve_safety_request(controller, trust)
        await resolve_safety_request(controller, approval)

    anyio.run(scenario)

    assert [command.model_dump(exclude_none=True) for command in controller.commands] == [
        {
            "id": "trust-1",
            "type": "trust",
            "request_id": "trust-request",
            "trusted": False,
            "reason": "The embedding application did not trust this project",
            "transient": True,
        },
        {
            "id": "approval-1",
            "type": "approval",
            "call_id": "tool-call",
            "approved": False,
            "reason": "The embedding application did not authorize this tool call",
        },
    ]


def test_control_example_emits_typed_requests_in_order() -> None:
    commands = anyio.run(build_control_requests)

    assert [command.type for command in commands] == [
        "prompt",
        "steer",
        "follow_up",
        "cancel",
        "compact",
    ]
    assert [json.loads(command.to_json_line())["id"] for command in commands] == [
        "prompt-1",
        "steer-1",
        "follow-up-1",
        "cancel-1",
        "compact-1",
    ]


def test_persisted_session_example_resumes_clones_and_forks(tmp_path: Path) -> None:
    result = anyio.run(
        persisted_session_workflow,
        tmp_path / "workspace",
        tmp_path / "sessions",
    )

    assert result["source"] != result["clone"]
    assert result["source"] != result["fork"]
    assert result["clone"] != result["fork"]
    assert result["editable_prompt"] == "persist this prompt"
    assert len(tuple((tmp_path / "sessions").glob("*.jsonl"))) == 3


@pytest.mark.process
def test_subprocess_rpc_example_runs_offline(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    inherited_home = tmp_path / "inherited-home"
    settings_dir = inherited_home / ".wisp"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.json").write_text(
        '{"mcp_servers":{"inherited":{"command":"must-not-run"}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(inherited_home))
    monkeypatch.setenv("WISP_OPENAI_COMPATIBLE_CONFIG", "not-json")

    response = anyio.run(
        prompt_in_subprocess,
        tmp_path / "workspace",
        tmp_path / "sessions",
    )

    assert response == "fake response to: hello over JSONL RPC"
