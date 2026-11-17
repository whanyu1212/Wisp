"""Validate the MCP guide's settings and run its codemode examples offline."""

import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.support.paths import DOCS_SRC_DIR
from wisp.config.settings import WispSettings
from wisp.tools.base import Tool, ToolInputSchema
from wisp.tools.codemode.calls import NestedCallResult
from wisp.tools.codemode.tool import CodemodeTool
from wisp.tools.context import ToolContext
from wisp.tools.files.operations import EditTool, ReadTool, WriteTool
from wisp.tools.shell.tool import BashTool

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
@pytest.mark.parametrize(
    "index",
    [
        pytest.param(0, id="inventory"),
        pytest.param(1, id="inspect"),
        pytest.param(2, id="edit-and-test", marks=pytest.mark.process),
        pytest.param(3, id="state"),
    ],
)
async def test_codemode_examples_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, index: int
) -> None:
    pytest.importorskip("pydantic_monty")
    examples = re.findall(r"```python\n(.*?)\n```", _GUIDE.read_text(encoding="utf-8"), re.DOTALL)
    assert len(examples) == 4
    (tmp_path / "settings.py").write_text("RETRY_SECONDS = 2\n", encoding="utf-8")
    if index == 2:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_settings.py").write_text(
            "from settings import RETRY_SECONDS\n\n"
            "def test_retry_interval():\n    assert RETRY_SECONDS == 4\n",
            encoding="utf-8",
        )
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "codemode-guide-example"\nversion = "0.0.0"\n',
            encoding="utf-8",
        )
        # Reuse the test environment without resolving or installing dependencies.
        monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", sys.prefix)
        monkeypatch.setenv("UV_NO_SYNC", "1")
        monkeypatch.setenv("UV_OFFLINE", "1")
        monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    context = ToolContext(cwd=tmp_path)
    local_tools: dict[str, Tool] = {
        tool.name: tool for tool in (ReadTool(), EditTool(), WriteTool(), BashTool())
    }
    calls: list[str] = []
    inventory_ids: list[str] = []

    async def call(name: str, arguments: Mapping[str, object]) -> NestedCallResult:
        calls.append(name)
        if name == _InventoryTool.name:
            item_id = str(arguments["item_id"])
            inventory_ids.append(item_id)
            return NestedCallResult(
                text=json.dumps({"id": item_id, "stock": 0 if item_id == "A" else 5})
            )
        result = await local_tools[name].run(arguments, context)
        return NestedCallResult(text=result.text)

    tool = CodemodeTool(list(local_tools.values()), call, discoverable=[_InventoryTool()])
    result = await tool.run({"code": examples[index]}, context)
    if index == 0:
        assert sorted(inventory_ids) == ["A", "B"]
        answer, summary = result.text.split("\n", 1)
        assert json.loads(answer) == [["A", 0]]
        assert summary == "[2 tool calls: mcp__inventory__get_item ×2]"
    elif index == 1:
        assert calls == ["read"]
        assert "RETRY_SECONDS = 2" in result.text
    elif index == 2:
        assert calls == ["edit", "write", "bash"]
        assert (tmp_path / "settings.py").read_text() == "RETRY_SECONDS = 4\n"
        assert (tmp_path / "retry-note.txt").read_text() == "Retry interval: 4 seconds\n"
        assert "1 passed" in result.text
        assert "Command exited with code 0" in result.text
    else:
        assert calls == []
        assert result.text == "['B']\ntrue\n[No tool calls.]"
