"""Validate the MCP guide's settings and run its codemode example offline."""

import json
import re
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.support.paths import DOCS_SRC_DIR
from wisp.config.settings import WispSettings
from wisp.tools.base import ToolInputSchema
from wisp.tools.codemode.calls import NestedCallResult
from wisp.tools.codemode.tool import CodemodeTool
from wisp.tools.context import ToolContext

_GUIDE = DOCS_SRC_DIR / "guide" / "mcp.md"


class _InventoryTool:
    name = "mcp__inventory__get_item"
    description = "Return an inventory item as JSON text."
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {"item_id": {"type": "string"}},
        "required": ["item_id"],
    }


def test_settings_examples_validate() -> None:
    examples = re.findall(r"```json\n(.*?)\n```", _GUIDE.read_text(encoding="utf-8"), re.DOTALL)
    assert examples
    for example in examples:
        data = json.loads(example)
        assert set(data) <= WispSettings.model_fields.keys()
        WispSettings.model_validate(data)


@pytest.mark.anyio
async def test_codemode_example_filters_discoverable_tool_results(tmp_path: Path) -> None:
    pytest.importorskip("pydantic_monty")
    examples = re.findall(r"```python\n(.*?)\n```", _GUIDE.read_text(encoding="utf-8"), re.DOTALL)
    assert len(examples) == 1
    calls: list[str] = []

    async def call(name: str, arguments: Mapping[str, object]) -> NestedCallResult:
        assert name == _InventoryTool.name
        item_id = str(arguments["item_id"])
        calls.append(item_id)
        return NestedCallResult(
            text=json.dumps({"id": item_id, "stock": 0 if item_id == "A" else 5})
        )

    tool = CodemodeTool([], call, discoverable=[_InventoryTool()])
    result = await tool.run({"code": examples[0]}, ToolContext(cwd=tmp_path))
    assert sorted(calls) == ["A", "B"]
    answer, summary = result.text.split("\n", 1)
    assert json.loads(answer) == [["A", 0]]
    assert summary == "[2 tool calls: mcp__inventory__get_item ×2]"
