"""Process-backed integration tests for MCP stdio runtime support."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from wisp.mcp.config import McpServerConfig
from wisp.runtime.extensions import build_runtime
from wisp.tools.context import ToolContext

pytestmark = pytest.mark.process


def _fixture_server(
    tmp_path: Path,
    *,
    name: str = "fixture",
    env: dict[str, str] | None = None,
    env_from: tuple[str, ...] = (),
) -> McpServerConfig:
    fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
    return McpServerConfig(
        name=name,
        command=sys.executable,
        args=("-u", str(fixture)),
        env={
            "WISP_MCP_TEST_CLOSED_FILE": str(tmp_path / f"{name}-closed"),
            **(env or {}),
        },
        env_from=env_from,
    )


def test_official_stdio_discovery_environment_invocation_and_cleanup(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("WISP_MCP_TEST_FORWARDED", "forwarded-value")
    monkeypatch.setenv("WISP_MCP_TEST_TRAP", "must-not-leak")
    server = _fixture_server(
        tmp_path,
        env={
            "WISP_MCP_TEST_LITERAL": "literal-value",
            "WISP_MCP_TEST_STDERR": "secret-stderr",
        },
        env_from=("WISP_MCP_TEST_FORWARDED",),
    )

    async def scenario() -> tuple[str, dict[str, object], tuple[str, ...]]:
        runtime = await build_runtime(mcp_servers=(server,))
        try:
            echo = runtime.tools.get("mcp__fixture__echo")
            environment = runtime.tools.get("mcp__fixture__runtime_environment")
            echo_result = await echo.run({"value": "hello"}, ToolContext(cwd=project))
            environment_result = await environment.run({}, ToolContext(cwd=project))
            return echo_result.text, json.loads(environment_result.text), runtime.tools.names()
        finally:
            await runtime.aclose()

    echo_text, environment, names = anyio.run(scenario)

    assert echo_text == "echo=hello"
    assert environment == {
        "cwd": str(Path.home().resolve()),
        "forwarded": "forwarded-value",
        "literal": "literal-value",
        "trap": None,
    }
    assert "mcp__fixture__echo" in names
    assert (tmp_path / "fixture-closed").read_text(encoding="utf-8") == "closed"
    captured = capfd.readouterr()
    assert "secret-stderr" not in captured.out
    assert "secret-stderr" not in captured.err


def test_unavailable_server_is_isolated_and_diagnostic_is_redacted(tmp_path: Path) -> None:
    secret = "credential-that-must-not-appear"
    healthy = _fixture_server(tmp_path, name="healthy")
    unavailable = McpServerConfig(
        name="broken",
        command=str(tmp_path / secret),
        env={"TOKEN": secret},
    )

    async def scenario() -> tuple[tuple[str, ...], tuple[str, ...]]:
        runtime = await build_runtime(mcp_servers=(unavailable, healthy))
        try:
            return (
                runtime.tools.names(),
                tuple(event.message for event in runtime.startup_events),
            )
        finally:
            await runtime.aclose()

    names, messages = anyio.run(scenario)

    assert "mcp__healthy__echo" in names
    assert messages == (
        "MCP server broken is unavailable: the server could not be started or initialized",
    )
    assert secret not in "\n".join(messages)
