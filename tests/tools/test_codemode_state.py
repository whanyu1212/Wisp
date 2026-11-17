"""Transactional script state and its actual Monty-facing helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import anyio
import pytest
from pydantic import JsonValue

from wisp.tools.base import ToolInputSchema
from wisp.tools.codemode.calls import NestedCallResult
from wisp.tools.codemode.sandbox import ScriptLimits
from wisp.tools.codemode.state import ScriptState
from wisp.tools.codemode.tool import CodemodeTool, callable_by_name
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError, ToolResult


@dataclass
class _Tool:
    name: str
    description: str = "A read-only query."

    @property
    def input_schema(self) -> ToolInputSchema:
        return {"type": "object", "properties": {}}


async def _query(name: str, arguments: Mapping[str, object]) -> NestedCallResult:
    return NestedCallResult(text="rows")


def _tool(state: ScriptState | None = None, *, limits: ScriptLimits | None = None) -> CodemodeTool:
    return CodemodeTool([_Tool("query")], _query, state=state, limits=limits)


def _run(tool: CodemodeTool, code: str) -> ToolResult:
    async def run() -> ToolResult:
        return await tool.run({"code": code}, ToolContext(cwd=Path(".")))

    return anyio.run(run)


def test_helpers_across_scripts() -> None:
    snapshots: list[dict[str, JsonValue]] = []
    state = ScriptState(on_commit=snapshots.append)
    tool = _tool(state)
    result = _run(tool, "await store('rows', {'names': ['a', 'b']})\nawait query()")

    assert result.data == {"calls": 1, "failed_calls": 0}
    assert result.text == "rows\n[1 tool call: query]"
    assert _run(tool, "await load('rows')").text.startswith('{"names": ["a", "b"]}')
    assert snapshots == [{"rows": {"names": ["a", "b"]}}]
    assert _run(tool, "[await delete('rows'), await delete('rows')]").text.startswith(
        "[true, false]"
    )
    assert state.snapshot() == {}
    assert snapshots[-1] == {}
    with pytest.raises(ToolError, match="KeyError"):
        _run(tool, "await load('rows')")


def test_helpers_copy_values() -> None:
    state = ScriptState()
    tool = _tool(state)
    _run(
        tool,
        "original = {'names': ['a']}\n"
        "await store('rows', original)\n"
        "original['names'].append('b')\n"
        "loaded = await load('rows')\n"
        "loaded['names'].append('c')\n"
        "await load('rows')",
    )
    assert state.snapshot() == {"rows": {"names": ["a"]}}
    snapshot = state.snapshot()
    snapshot.clear()
    assert state.snapshot() == {"rows": {"names": ["a"]}}


@pytest.mark.parametrize("code", ["raise ValueError('broken')", "1 / 0", "await load('missing')"])
def test_failure_discards_writes(code: str) -> None:
    snapshots: list[dict[str, JsonValue]] = []
    state = ScriptState({"existing": 1}, on_commit=snapshots.append)
    with pytest.raises(ToolError, match="Script failed"):
        _run(_tool(state), f"await store('existing', 2)\nawait store('new', 3)\n{code}")
    assert state.snapshot() == {"existing": 1}
    assert snapshots == []
    _run(_tool(state), "await store('next', True)")
    assert state.snapshot() == {"existing": 1, "next": True}


@pytest.mark.parametrize("ending", ["timeout", "cancellation"])
def test_interruption_discards_writes(ending: str) -> None:
    state = ScriptState({"existing": 1})

    async def run() -> None:
        started = anyio.Event()

        async def wait(name: str, arguments: Mapping[str, object]) -> NestedCallResult:
            started.set()
            await anyio.sleep_forever()

        tool = CodemodeTool(
            [_Tool("query")],
            wait,
            state=state,
            limits=ScriptLimits(wall_seconds=0.3 if ending == "timeout" else 10),
        )

        async def script() -> None:
            await tool.run(
                {"code": "await store('existing', 2)\nawait query()"},
                ToolContext(cwd=Path(".")),
            )

        if ending == "timeout":
            with pytest.raises(ToolError, match=r"Script failed \(timeout\)"):
                await script()
        else:
            async with anyio.create_task_group() as group:
                group.start_soon(script)
                await started.wait()
                group.cancel_scope.cancel()

    anyio.run(run)
    assert state.snapshot() == {"existing": 1}
    _run(_tool(state), "await store('next', True)")
    assert state.snapshot() == {"existing": 1, "next": True}


@pytest.mark.parametrize(
    "value", [(), {1: "bad"}, float("nan"), float("inf"), {"nested": [float("-inf")]}, object()]
)
def test_rejects_non_json(value: object) -> None:
    async def run() -> None:
        transaction = ScriptState().transaction()
        with pytest.raises(ValueError, match="JSON"):
            await transaction.store("x", value)
        transaction.close()

    anyio.run(run)


@pytest.mark.parametrize("value", ["(1, 2)", "{1: 'bad'}", "float('nan')", "float('inf')"])
def test_monty_rejects_non_json(value: str) -> None:
    state = ScriptState()
    with pytest.raises(ToolError, match="JSON"):
        _run(_tool(state), f"await store('x', {value})")
    assert state.snapshot() == {}


def test_boundaries_copy_values() -> None:
    initial: dict[str, JsonValue] = {"x": [1]}

    def mutate(snapshot: dict[str, JsonValue]) -> None:
        snapshot.clear()

    state = ScriptState(initial, on_commit=mutate)
    initial.clear()
    assert state.snapshot() == {"x": [1]}

    async def run() -> None:
        transaction = state.transaction()
        value = [2]
        await transaction.store("x", value)
        value.append(3)
        loaded = await transaction.load("x")
        assert isinstance(loaded, list)
        loaded.append(4)
        transaction.commit()

    anyio.run(run)
    assert state.snapshot() == {"x": [2]}


@pytest.mark.parametrize("key", ["", "é" * 65, "a" * 129])
def test_rejects_invalid_keys(key: str) -> None:
    with pytest.raises(ValueError, match="keys"):
        ScriptState({key: None})


def test_enforces_state_quotas() -> None:
    async def run() -> None:
        state = ScriptState({str(index): None for index in range(128)})
        transaction = state.transaction()
        with pytest.raises(ValueError, match="128 keys"):
            await transaction.store("overflow", None)
        await transaction.store("0", "é" * 32767)
        with pytest.raises(ValueError, match="65536 JSON bytes"):
            await transaction.store("0", "é" * 32768)
        transaction.commit()
        assert state.snapshot()["0"] == "é" * 32767

        transaction = ScriptState().transaction()
        for key in ["a", "b", "c"]:
            await transaction.store(key, "x" * 65534)
        with pytest.raises(ValueError, match="262144 JSON bytes"):
            await transaction.store("d", "x" * 65534)
        await transaction.store("d", "x" * 65500)
        transaction.commit()

        nested: JsonValue = None
        for _ in range(32):
            nested = [nested]
        transaction = ScriptState().transaction()
        await transaction.store("allowed", nested)
        with pytest.raises(ValueError, match="32 containers"):
            await transaction.store("deep", [nested])
        transaction.commit()

    anyio.run(run)


def test_callback_precedes_publication() -> None:
    state: ScriptState

    def fail(snapshot: dict[str, JsonValue]) -> None:
        assert snapshot == {"x": 2}
        assert state.snapshot() == {"x": 1}
        raise OSError("disk full")

    state = ScriptState({"x": 1}, on_commit=fail)
    with pytest.raises(OSError, match="disk full"):
        _run(_tool(state), "await store('x', 2)")
    assert state.snapshot() == {"x": 1}
    _run(_tool(state), "await load('x')")


def test_noop_skips_callback() -> None:
    snapshots: list[dict[str, JsonValue]] = []
    state = ScriptState({"x": 1}, on_commit=snapshots.append)
    tool = _tool(state)
    _run(tool, "await load('x')")
    _run(tool, "await store('temp', [])\nawait delete('temp')\nawait store('x', 1)")
    assert snapshots == []
    _run(tool, "await store('x', True)")
    assert snapshots == [{"x": True}]


def test_transaction_lifecycle() -> None:
    async def run() -> None:
        state = ScriptState()
        transaction = state.transaction()
        transaction.close()
        next_transaction = state.transaction()
        transaction.close()
        for operation in [
            transaction.store("x", 1),
            transaction.load("x"),
            transaction.delete("x"),
        ]:
            with pytest.raises(RuntimeError, match="has ended"):
                await operation
        with pytest.raises(RuntimeError, match="has ended"):
            transaction.commit()
        next_transaction.commit()
        with pytest.raises(RuntimeError, match="has ended"):
            await next_transaction.store("x", 1)

    anyio.run(run)


def test_concurrent_writes_conflict() -> None:
    snapshots: list[dict[str, JsonValue]] = []
    state = ScriptState(on_commit=snapshots.append)

    async def run() -> None:
        left = state.transaction()
        right = state.transaction()
        read_only = state.transaction()
        await left.store("left", 1)
        await right.store("right", 2)
        left.commit()
        read_only.commit()
        with pytest.raises(RuntimeError, match="changed while"):
            right.commit()
        with pytest.raises(RuntimeError, match="has ended"):
            await right.store("right", 3)

    anyio.run(run)
    assert state.snapshot() == {"left": 1}
    assert snapshots == [{"left": 1}]


def test_callback_failure_preserves_revision() -> None:
    attempts = 0

    def fail_once(snapshot: dict[str, JsonValue]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("disk full")

    state = ScriptState({"x": 1}, on_commit=fail_once)

    async def run() -> None:
        first = state.transaction()
        second = state.transaction()
        await first.store("x", 2)
        await second.store("x", 3)
        with pytest.raises(OSError, match="disk full"):
            first.commit()
        assert state.snapshot() == {"x": 1}
        second.commit()

    anyio.run(run)
    assert state.snapshot() == {"x": 3}
    assert attempts == 2


@pytest.mark.parametrize(
    "name", ["store", "load", "delete", "gather_settled", "_asyncio", "_TOOL_NAMES"]
)
def test_helper_names_reserved(name: str) -> None:
    collision = _Tool(name)
    assert not callable_by_name(collision)
    for tool in [
        CodemodeTool([collision], _query),
        CodemodeTool([], _query, discoverable=[collision]),
    ]:
        assert tool.tool_names == ()
        result = _run(tool, "await store('x', {'rows': [1]})\nawait load('x')")
        assert json.loads(result.text.rsplit("\n", 1)[0]) == {"rows": [1]}
        assert result.data == {"calls": 0, "failed_calls": 0}


def test_default_state_is_ephemeral() -> None:
    tool = _tool()
    _run(tool, "await store('x', [1])")
    assert _run(tool, "await load('x')").text.startswith("[1]")
    with pytest.raises(ToolError, match="KeyError"):
        _run(_tool(), "await load('x')")
