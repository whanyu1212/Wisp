"""Recover truncated script text without rerunning its nested calls."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from pathlib import Path

import anyio
import pytest

from wisp.tools.codemode.calls import NestedCallResult
from wisp.tools.codemode.sandbox import ScriptLimits, ScriptRun
from wisp.tools.codemode.state import ScriptState
from wisp.tools.codemode.tool import CodemodeTool
from wisp.tools.context import ToolContext
from wisp.tools.output_store import OutputStore, SavedStream, SaveStatus
from wisp.tools.result import ToolError, ToolResult


@pytest.fixture
def store(tmp_path: Path) -> Iterator[OutputStore]:
    output_store = OutputStore(tmp_path / "output")
    yield output_store
    anyio.run(output_store.aclose)


async def _unexpected_call(name: str, arguments: Mapping[str, object]) -> NestedCallResult:
    raise AssertionError(f"Unexpected tool call: {name}")


def _tool(store: OutputStore | None, state: ScriptState | None = None) -> CodemodeTool:
    return CodemodeTool([], _unexpected_call, output_store=store, state=state)


def _run(tool: CodemodeTool, code: str, context: ToolContext) -> ToolResult:
    async def run() -> ToolResult:
        return await tool.run({"code": code}, context)

    return anyio.run(run)


def _read(store: OutputStore, output_id: str) -> tuple[str, ...]:
    async def read() -> tuple[str, ...]:
        page = await store.read_page(
            output_id, offset=1, limit=None, max_bytes=100_000, max_lines=10_000
        )
        assert page.complete
        return page.lines

    return anyio.run(read)


def test_small_output_stays_inline(store: OutputStore, tmp_path: Path) -> None:
    state = ScriptState()
    result = _run(
        _tool(store, state),
        "await store('done', True)\nprint('printed')\n'final'",
        ToolContext(cwd=tmp_path),
    )

    assert result.text == "printed\nfinal\n[No tool calls.]"
    assert result.data == {"calls": 0, "failed_calls": 0}
    assert not result.truncated
    assert state.snapshot() == {"done": True}
    assert not (tmp_path / "output").exists()


def test_recovers_printed_and_final(store: OutputStore, tmp_path: Path) -> None:
    result = _run(
        _tool(store),
        "for n in range(100):\n    print(f'行{n}')\n'final expression'",
        ToolContext(cwd=tmp_path, max_output_bytes=300, max_output_lines=8),
    )

    output_id = result.data["output_id"]
    assert isinstance(output_id, str)
    assert result.truncated
    assert f"output_id={output_id}" in result.text
    assert "read_output" in result.text
    assert "[No tool calls.]" in result.text
    assert len(result.text.encode()) <= 300
    assert len(result.text.splitlines()) <= 8
    assert _read(store, output_id) == (*(f"行{n}" for n in range(100)), "final expression")


def test_failure_recovers_without_commit(store: OutputStore, tmp_path: Path) -> None:
    state = ScriptState({"keep": True})
    code = (
        "await store('pending', True)\n"
        "for n in range(500):\n    print(f'row {n}')\n"
        "raise ValueError('broken' * 1000)"
    )
    with pytest.raises(ToolError) as failure:
        _run(_tool(store, state), code, ToolContext(cwd=tmp_path))

    message = str(failure.value)
    assert len(message) <= 1_900
    assert "Script failed (runtime): ValueError: broken" in message
    assert "Calls made before it failed: No tool calls." in message
    match = re.search(r"output_id=([a-f0-9]+)", message)
    assert match is not None
    assert _read(store, match[1]) == tuple(f"row {n}" for n in range(500))
    assert state.snapshot() == {"keep": True}


def test_artifact_obeys_store_cap(
    store: OutputStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("wisp.tools.output_store.MAX_SAVED_STREAM_BYTES", 64)
    result = _run(_tool(store), "'x' * 2000", ToolContext(cwd=tmp_path, max_output_bytes=500))

    output_id = result.data["output_id"]
    assert isinstance(output_id, str)
    assert _read(store, output_id) == ("x" * 64,)
    assert "1936 later bytes not saved" in result.text


def test_final_expression_cap_recovers(store: OutputStore, tmp_path: Path) -> None:
    state = ScriptState()
    tool = CodemodeTool(
        [],
        _unexpected_call,
        output_store=store,
        state=state,
        limits=ScriptLimits(max_printed_bytes=64),
    )
    with pytest.raises(ToolError) as failure:
        _run(
            tool,
            "await store('pending', True)\nprint('first')\n'x' * 1000",
            ToolContext(cwd=tmp_path),
        )

    message = str(failure.value)
    assert "Script failed (limit)" in message
    match = re.search(r"output_id=([a-f0-9]+)", message)
    assert match is not None
    retained = "\n".join(_read(store, match[1]))
    assert retained.startswith("first\nx")
    assert len(retained.encode()) <= 64
    assert state.snapshot() == {}


@pytest.mark.parametrize("failed", [False, True], ids=["success", "failure"])
def test_storage_failure_is_explicit(tmp_path: Path, failed: bool) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    output_store = OutputStore(blocked)
    state = ScriptState()
    try:
        ending = "print('x' * 2000)\nraise ValueError('broken')" if failed else "'x' * 2000"
        code = f"await store('done', True)\n{ending}"
        context = ToolContext(cwd=tmp_path, max_output_bytes=500)
        if failed:
            with pytest.raises(ToolError) as failure:
                _run(_tool(output_store, state), code, context)
            text = str(failure.value)
        else:
            result = _run(_tool(output_store, state), code, context)
            assert result.truncated
            assert "output_id" not in result.data
            text = result.text
        assert "could not be saved" in text
        assert "read_output" not in text
        assert state.snapshot() == ({} if failed else {"done": True})
    finally:
        anyio.run(output_store.aclose)


def test_missing_store_is_explicit(tmp_path: Path) -> None:
    result = _run(_tool(None), "'x' * 2000", ToolContext(cwd=tmp_path, max_output_bytes=500))

    assert result.truncated
    assert "could not be saved" in result.text
    assert "recovery is unavailable" in result.text
    assert "output_id" not in result.data


@pytest.mark.parametrize(("max_bytes", "max_lines"), [(0, 0), (5, 1), (50, 1), (100, 2)])
def test_tiny_preview_stays_bounded(
    store: OutputStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    max_bytes: int,
    max_lines: int,
) -> None:
    async def finished(*args: object, **kwargs: object) -> ScriptRun:
        return ScriptRun(printed="x" * 2000)

    monkeypatch.setattr("wisp.tools.codemode.tool.run_script", finished)
    result = _run(
        _tool(store),
        "pass",
        ToolContext(cwd=tmp_path, max_output_bytes=max_bytes, max_output_lines=max_lines),
    )

    assert result.truncated
    assert len(result.text.encode()) <= max_bytes
    assert len(result.text.splitlines()) <= max_lines
    output_id = result.data["output_id"]
    assert isinstance(output_id, str)
    assert _read(store, output_id) == ("x" * 2000,)
    if max_bytes >= 50:
        assert f"output_id={output_id}" in result.text


def test_cancellation_discards_artifact(
    store: OutputStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = ScriptState()
    saved = SavedStream.save

    async def run() -> None:
        started = anyio.Event()

        async def hold(stream: SavedStream) -> SaveStatus | None:
            await saved(stream)
            started.set()
            await anyio.sleep_forever()

        monkeypatch.setattr(SavedStream, "save", hold)

        async def script() -> None:
            await _tool(store, state).run(
                {"code": "await store('pending', True)\n'x' * 2000"},
                ToolContext(cwd=tmp_path, max_output_bytes=500),
            )

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(script)
            await started.wait()
            tasks.cancel_scope.cancel()

    anyio.run(run)
    assert state.snapshot() == {}
    assert list((tmp_path / "output").rglob("*.log")) == []
