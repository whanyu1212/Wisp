"""Synthetic MCP servers for the MCP tasks (`evals.tool_use.servers`)."""

from __future__ import annotations

import sys
from pathlib import Path

import anyio
import pytest

from evals.tool_use.runner import mcp_settings, mcp_tool_names
from evals.tool_use.servers.catalogs import (
    CATALOGS,
    INVOICE_ID,
    LIVE_TOTAL,
    REORDER_POINT,
    REORDER_SKU,
    REORDER_WAREHOUSE,
)
from evals.tool_use.task_definitions import Task, select_tasks
from wisp.config.settings import WispSettings
from wisp.runtime.extensions import build_runtime
from wisp.tools.context import ToolContext

_MCP_TASKS = select_tasks(tags=["mcp"])


def _solution(task_id: str) -> str:
    task = next(task for task in _MCP_TASKS if task.id == task_id)
    return (task.folder / "solution" / "ANSWER.txt").read_text(encoding="utf-8").strip()


def test_answers_come_from_catalogs() -> None:
    policy = CATALOGS["inventory"].tools[0]
    billing_totals = {
        name: CATALOGS[name].tools[0].respond({"invoice_id": INVOICE_ID})
        for name in ("billing", "billing-legacy", "billing-sandbox")
    }

    assert policy.name == "get_reorder_policy"
    answer = policy.respond({"sku": REORDER_SKU, "warehouse": REORDER_WAREHOUSE.title()})
    assert f"reorder point {REORDER_POINT} units" in answer
    assert _solution("mcp-pick-tool") == str(REORDER_POINT)
    assert _solution("mcp-similar-servers") == LIVE_TOTAL
    assert LIVE_TOTAL in billing_totals["billing"]
    assert not any(LIVE_TOTAL in text for name, text in billing_totals.items() if name != "billing")


def test_inventory_decoys_differ() -> None:
    target = {"sku": REORDER_SKU, "warehouse": REORDER_WAREHOUSE}
    decoys = {tool.name: tool.respond(target) for tool in CATALOGS["inventory"].tools[1:]}

    assert not [name for name, text in decoys.items() if f" {REORDER_POINT} " in text]


@pytest.mark.parametrize("sku", [REORDER_SKU, " a-1172 "])
def test_reorder_point_ignores_spelling(sku: str) -> None:
    policy = CATALOGS["inventory"].tools[0]

    assert f"reorder point {REORDER_POINT} units" in policy.respond(
        {"sku": sku, "warehouse": " Lisbon"}
    )


@pytest.mark.process
def test_tool_call_through_wisp(tmp_path: Path) -> None:
    task = next(task for task in _MCP_TASKS if task.id == "mcp-pick-tool")
    servers = WispSettings.model_validate(mcp_settings(task, sys.executable)).mcp_servers

    async def call() -> str:
        runtime = await build_runtime(mcp_servers=servers or ())
        try:
            tool = runtime.tools.get("mcp__inventory__get_reorder_policy")
            result = await tool.run(
                {"sku": REORDER_SKU, "warehouse": "Lisbon"}, ToolContext(cwd=tmp_path)
            )
            return result.text
        finally:
            await runtime.aclose()

    assert f"reorder point {REORDER_POINT} units" in anyio.run(call)


def test_inventory_has_fifty_tools() -> None:
    names = CATALOGS["inventory"].tool_names

    assert len(names) == len(set(names)) == 50


@pytest.mark.process
@pytest.mark.parametrize("task", _MCP_TASKS, ids=[task.id for task in _MCP_TASKS])
def test_servers_start_through_wisp(task: Task, tmp_path: Path) -> None:
    servers = WispSettings.model_validate(mcp_settings(task, sys.executable)).mcp_servers

    async def offered() -> tuple[str, ...]:
        runtime = await build_runtime(mcp_servers=servers or ())
        try:
            return runtime.tools.names()
        finally:
            await runtime.aclose()

    assert set(mcp_tool_names(task)) <= set(anyio.run(offered))
