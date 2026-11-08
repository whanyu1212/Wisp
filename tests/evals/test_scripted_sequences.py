"""The scripted layer of the tool-use evals: fixed tool-call sequences on real tools.

The live suite (`evals/tool_use/`) measures what models do with Wisp's tools; these
tests pin the tool behavior those measurements rely on, with a scripted model, so
they run in CI, in well under a second together. Each script plays one recovery
path the live tasks exercise: reading output a result cut, retrying an edit that
failed, and finding and calling an MCP tool. A script step may read the previous
tool result, as a model would.
"""

from __future__ import annotations

import itertools
import re
import shlex
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import anyio
import pytest

from evals.tool_use.runner import mcp_settings
from evals.tool_use.servers.catalogs import REORDER_POINT, REORDER_SKU
from evals.tool_use.task_definitions import select_tasks
from tests.agent.loop.support import tool_call_stream
from wisp.coding.session import CodingSession
from wisp.config.settings import WispSettings
from wisp.events import AgentCompleted, ToolResultReady, WispEvent
from wisp.messages import Message
from wisp.providers.base import ToolCallResult, ToolSpec
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ToolCall,
)
from wisp.runtime.extensions import build_runtime
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.context import ToolContext
from wisp.tools.selection import select_tools

_CALL_NUMBERS = itertools.count(1)


@dataclass(frozen=True, slots=True)
class _Request:
    """What the scripted model sees before it answers: the tools and the last result."""

    tools: tuple[ToolSpec, ...]
    last_result: str


type _Step = Callable[[_Request], Sequence[ProviderEvent]]


class _ScriptedModel:
    """Answers each request with the next step, which may read the last tool result."""

    name = "scripted"
    default_model: str | None = "scripted"
    supports_continuation_messages = True
    supports_context_rebase = True

    def __init__(self, steps: Sequence[_Step]) -> None:
        self._steps = list(steps)

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        extra_messages: Sequence[Message] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
        prompt_cache_key: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        assert self._steps, "the script ran out of steps"
        last = tool_results[-1].output if tool_results else ""
        for event in self._steps.pop(0)(_Request(tools=tuple(tools), last_result=last)):
            await anyio.sleep(0)
            yield event


def _call(name: str, **arguments: object) -> _Step:
    return lambda _request: _tool_call(name, arguments)


def _tool_call(name: str, arguments: dict[str, object]) -> list[ProviderEvent]:
    call_id = f"call-{next(_CALL_NUMBERS)}"
    return tool_call_stream(ToolCall(call_id=call_id, name=name, arguments=arguments))


def _answer(text: str) -> _Step:
    return lambda _request: [
        ProviderResponseStarted(model="scripted"),
        ProviderResponseCompleted(content=text),
    ]


async def _run(
    tmp_path: Path, steps: Sequence[_Step], *, tools: Sequence[str], settings: dict[str, object]
) -> list[WispEvent]:
    servers = WispSettings.model_validate(settings).mcp_servers or ()
    runtime = await build_runtime(mcp_servers=servers)
    try:
        session = CodingSession(
            provider=_ScriptedModel(steps),
            sessions=JsonlSessionStore(tmp_path / "sessions"),
            tool_registry=select_tools(runtime.tools, allowed_tools=tuple(tools)),
            tool_context=ToolContext(cwd=tmp_path),
            tool_approval_policy=ToolApprovalPolicy.approve_all(),
        )
        return [event async for event in session.run("go")]
    finally:
        await runtime.aclose()


def _results(events: Sequence[WispEvent]) -> list[ToolResultReady]:
    return [event for event in events if isinstance(event, ToolResultReady)]


def _outcome(events: Sequence[WispEvent]) -> str:
    (completed,) = [event for event in events if isinstance(event, AgentCompleted)]
    return completed.outcome


@pytest.mark.process
def test_cut_output_read_back(tmp_path: Path) -> None:
    code = (
        "print('error: first failure')\n"
        "for n in range(1, 5001):\n"
        "    print('MISMATCH account 4810' if n == 2500 else f'ok {n}')"
    )

    def read_middle(request: _Request) -> list[ProviderEvent]:
        (output_id,) = re.findall(r"stdout output_id=([0-9a-f]+)", request.last_result)
        return _tool_call("read_output", {"output_id": output_id, "offset": 2501, "limit": 1})

    steps = [
        _call("bash", command=f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"),
        read_middle,
        _answer("Account 4810 is the mismatch."),
    ]

    events = anyio.run(lambda: _run(tmp_path, steps, tools=["bash"], settings={}))

    bash, read_output = _results(events)
    assert bash.truncated and "error: first failure" in bash.output
    assert "MISMATCH" not in bash.output
    assert "MISMATCH account 4810" in read_output.output and not read_output.is_error
    assert _outcome(events) == "completed"


def test_failed_edit_recovers(tmp_path: Path) -> None:
    handlers = tmp_path / "handlers.py"
    handlers.write_text(
        "def handle_login(request):\n    return send(request, timeout=30)\n\n\n"
        "def handle_refund(request):\n    return send(request, timeout=30)\n",
        encoding="utf-8",
    )
    unique = "handle_refund(request):\n    return send(request, timeout=30)"
    steps = [
        _call("read", path="handlers.py"),
        _call(
            "edit", path="handlers.py", edits=[{"oldText": "timeout=30", "newText": "timeout=90"}]
        ),
        _call(
            "edit",
            path="handlers.py",
            edits=[{"oldText": unique, "newText": unique.replace("=30", "=90")}],
        ),
        _answer("Raised the refund timeout."),
    ]

    events = anyio.run(lambda: _run(tmp_path, steps, tools=["read", "edit"], settings={}))

    _read, ambiguous, fixed = _results(events)
    assert (ambiguous.is_error, ambiguous.failure_code, ambiguous.retryable) == (
        True,
        "invalid_arguments",
        True,
    )
    assert ambiguous.recovery_hint is not None and "unique" in ambiguous.recovery_hint
    assert not fixed.is_error
    text = handlers.read_text(encoding="utf-8")
    assert text.count("timeout=30") == 1 and text.endswith("timeout=90)\n")
    assert _outcome(events) == "completed"


@pytest.mark.process
def test_mcp_tool_found_and_called(tmp_path: Path) -> None:
    task = next(task for task in select_tasks(tags=["mcp"]) if task.id == "mcp-pick-tool")
    offered: list[str] = []

    def call_policy(request: _Request) -> list[ProviderEvent]:
        offered.extend(spec.name for spec in request.tools)
        name = next(name for name in offered if name.endswith("__get_reorder_policy"))
        return _tool_call(name, {"sku": REORDER_SKU, "warehouse": "Lisbon"})

    steps = [call_policy, _answer(str(REORDER_POINT))]
    settings = mcp_settings(task, sys.executable)

    events = anyio.run(
        lambda: _run(
            tmp_path, steps, tools=["mcp__inventory__get_reorder_policy"], settings=settings
        )
    )

    (policy,) = _results(events)
    assert offered == ["mcp__inventory__get_reorder_policy"]
    assert f"reorder point {REORDER_POINT} units" in policy.output and not policy.is_error
    assert _outcome(events) == "completed"
