from __future__ import annotations

from pathlib import Path

import anyio

from wisp.coding.session import CodingSession
from wisp.events import ToolExecutionEnded
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderToolCallCompleted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.skills.models import SkillCatalog, SkillEntry
from wisp.skills.tool import SkillTool
from wisp.tools.base import ToolArguments, ToolInputSchema, ToolSafety
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolResult


class _ReplacementSkillTool:
    name = "skill"
    safety: ToolSafety = "read"
    description = "Extension replacement for the built-in skill tool."
    input_schema: ToolInputSchema = {"type": "object", "properties": {}}

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        return ToolResult(text="replacement result")


def _skill(tmp_path: Path) -> SkillEntry:
    root = tmp_path / "demo"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo instructions\n---\nUse the narrow workflow.\n",
        encoding="utf-8",
    )
    return SkillEntry(
        name="demo",
        description="Demo instructions",
        source="user:wisp",
        root=root,
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(SkillTool())
    return registry


def test_coding_session_sends_bounded_skill_index_and_tool(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [[ProviderResponseStarted(model="scripted"), ProviderResponseCompleted(content="done")]]
    )
    entry = _skill(tmp_path)
    agent = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=_registry(),
        tool_context=ToolContext(cwd=tmp_path),
        skill_catalog=SkillCatalog(entries=(entry,)),
    )

    async def scenario() -> None:
        _ = [event async for event in agent.run("help")]

    anyio.run(scenario)

    request = provider.calls[0]
    assert tuple(tool.name for tool in request.tools) == ("skill",)
    skill_messages = [
        message.content for message in request.messages if "[WISP AGENT SKILLS]" in message.content
    ]
    assert len(skill_messages) == 1
    assert '"name":"demo"' in skill_messages[0]
    assert str(entry.root) not in skill_messages[0]


def test_coding_session_omits_skill_index_when_tool_is_not_exposed(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [[ProviderResponseStarted(model="scripted"), ProviderResponseCompleted(content="done")]]
    )
    agent = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_context=ToolContext(cwd=tmp_path),
        skill_catalog=SkillCatalog(entries=(_skill(tmp_path),)),
    )

    async def scenario() -> None:
        _ = [event async for event in agent.run("help")]

    anyio.run(scenario)

    request = provider.calls[0]
    assert request.tools == ()
    assert all("[WISP AGENT SKILLS]" not in message.content for message in request.messages)


def test_operation_bound_skill_tool_loads_from_same_catalog(tmp_path: Path) -> None:
    call = ToolCall(call_id="skill-1", name="skill", arguments={"name": "demo"})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="scripted"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                ),
            ],
            [ProviderResponseStarted(model="scripted"), ProviderResponseCompleted(content="done")],
        ]
    )
    entry = _skill(tmp_path)
    agent = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=_registry(),
        tool_context=ToolContext(cwd=tmp_path),
        skill_catalog=SkillCatalog(entries=(entry,)),
    )

    async def scenario() -> list[object]:
        return [event async for event in agent.run("use the demo skill")]

    events = anyio.run(scenario)

    result = next(event for event in events if isinstance(event, ToolExecutionEnded))
    assert result.output == "Use the narrow workflow.\n"
    assert result.is_error is False
    assert provider.calls[1].tool_results[0].output == "Use the narrow workflow.\n"


def test_extension_replacement_named_skill_is_not_overwritten(tmp_path: Path) -> None:
    call = ToolCall(call_id="skill-1", name="skill", arguments={})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="scripted"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                ),
            ],
            [ProviderResponseStarted(model="scripted"), ProviderResponseCompleted(content="done")],
        ]
    )
    registry = ToolRegistry()
    registry.register(_ReplacementSkillTool())
    agent = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=registry,
        tool_context=ToolContext(cwd=tmp_path),
        skill_catalog=SkillCatalog(entries=(_skill(tmp_path),)),
    )

    async def scenario() -> list[object]:
        return [event async for event in agent.run("use the replacement")]

    events = anyio.run(scenario)

    result = next(event for event in events if isinstance(event, ToolExecutionEnded))
    assert result.output == "replacement result"
    assert all(
        "[WISP AGENT SKILLS]" not in message.content for message in provider.calls[0].messages
    )
