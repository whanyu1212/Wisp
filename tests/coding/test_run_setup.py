"""The setup a run reports on `agent.started`, which lets runs be compared."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import anyio
import pytest

from wisp.coding.session import CodingSession
from wisp.events import AgentStarted, RunSetup
from wisp.messages import Message
from wisp.providers.base import ToolSpec
from wisp.providers.fake import FakeProvider
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.context import ToolContext

_READ = ToolSpec(
    name="read",
    description="Read a file.",
    input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
)
_GREP = ToolSpec(
    name="grep",
    description="Search files.",
    input_schema={"type": "object", "properties": {"pattern": {"type": "string"}}},
)
_INSTRUCTIONS = (Message(role="system", content="You are a coding agent."),)


def _setup(
    tmp_path: Path,
    tools: Sequence[ToolSpec],
    instructions: Sequence[Message] = _INSTRUCTIONS,
) -> RunSetup:
    async def scenario() -> RunSetup:
        agent = CodingSession(
            provider=FakeProvider(),
            sessions=JsonlSessionStore(tmp_path / "sessions"),
            tools=tools,
            prompt_messages=instructions,
            tool_context=ToolContext(cwd=tmp_path),
        )
        events = [event async for event in agent.run("hello")]
        started = next(event for event in events if isinstance(event, AgentStarted))
        assert started.setup is not None
        return started.setup

    return anyio.run(scenario)


def test_names_offered_tools(tmp_path: Path) -> None:
    setup = _setup(tmp_path, [_READ, _GREP])

    assert setup.tool_names == ("read", "grep")


def test_same_setup_same_hashes(tmp_path: Path) -> None:
    first = _setup(tmp_path / "first", [_READ, _GREP])
    second = _setup(tmp_path / "second", [_READ, _GREP])

    assert first == second


@pytest.mark.parametrize(
    ("tools", "instructions", "changed"),
    [
        pytest.param([_GREP, _READ], _INSTRUCTIONS, "tools", id="reordered"),
        pytest.param(
            [_READ, ToolSpec(_GREP.name, "Search file contents.", _GREP.input_schema)],
            _INSTRUCTIONS,
            "tools",
            id="reworded",
        ),
        pytest.param(
            [_READ, ToolSpec(_GREP.name, _GREP.description, {"type": "object"})],
            _INSTRUCTIONS,
            "tools",
            id="new-schema",
        ),
        pytest.param(
            [_READ, _GREP],
            (Message(role="system", content="You are a careful coding agent."),),
            "instructions",
            id="new-instructions",
        ),
        pytest.param(
            [
                ToolSpec(
                    _READ.name,
                    _READ.description,
                    {"properties": {"path": {"type": "string"}}, "type": "object"},
                ),
                _GREP,
            ],
            _INSTRUCTIONS,
            "nothing",
            id="schema-keys-reordered",
        ),
    ],
)
def test_hashes_track_changes(
    tmp_path: Path,
    tools: list[ToolSpec],
    instructions: tuple[Message, ...],
    changed: str,
) -> None:
    baseline = _setup(tmp_path / "baseline", [_READ, _GREP])
    variant = _setup(tmp_path / "variant", tools, instructions)

    tools_moved = variant.tools_sha256 != baseline.tools_sha256
    instructions_moved = variant.instructions_sha256 != baseline.instructions_sha256
    assert (tools_moved, instructions_moved) == (changed == "tools", changed == "instructions")
