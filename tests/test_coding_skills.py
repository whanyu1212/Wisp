from __future__ import annotations

from pathlib import Path

import anyio

from wisp.coding.session import CodingSession
from wisp.events import QueueMessageInjected, SkillInvoked, ToolExecutionEnded, wisp_event_from_json
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderToolCallCompleted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.skills.models import SkillCatalog, SkillEntry
from wisp.skills.tool import SkillTool
from wisp.tools.base import ToolArguments, ToolInputSchema, ToolSafety
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolResult


class _ReplacementSkillTool(SkillTool):
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


def test_explicit_skill_invocation_is_expanded_persisted_and_emitted(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [[ProviderResponseStarted(model="scripted"), ProviderResponseCompleted(content="done")]]
    )
    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()
    entry = _skill(tmp_path)
    agent = CodingSession(
        provider=provider,
        sessions=store,
        tool_context=ToolContext(cwd=tmp_path),
        skill_catalog=SkillCatalog(entries=(entry,)),
    )

    async def scenario() -> list[object]:
        return [event async for event in agent.run("/skill:demo inspect this", session=session)]

    events = anyio.run(scenario)

    request_message = provider.calls[0].messages[-1]
    assert request_message.content.startswith("[WISP EXPLICIT SKILL]\nSkill: demo")
    assert request_message.content.endswith("[USER REQUEST]\ninspect this")
    assert request_message.skill_invocation is not None
    assert request_message.skill_invocation.original_content == "/skill:demo inspect this"
    persisted = session.read_context_messages()[-2]
    assert persisted == request_message
    invoked = next(event for event in events if isinstance(event, SkillInvoked))
    assert invoked.message_entry_id
    assert invoked.invocation == request_message.skill_invocation
    assert invoked.provider_content == request_message.content
    assert wisp_event_from_json(invoked.model_dump_json()) == invoked
    rpc_message = next(
        message for message in session.read_message_page().messages if message.skill_invocation
    )
    assert rpc_message.skill_invocation is not None
    assert rpc_message.skill_invocation.original_content == "/skill:demo inspect this"

    (entry.root / "SKILL.md").unlink()
    replayed = session.read_context_messages()[-2]
    assert replayed == request_message


def test_queued_skill_invocation_uses_same_expansion_policy(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [ProviderResponseStarted(model="scripted"), ProviderResponseCompleted(content="first")],
            [
                ProviderResponseStarted(model="scripted"),
                ProviderResponseCompleted(content="second"),
            ],
        ]
    )
    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()
    entry = _skill(tmp_path)
    event_bus = EventBus()
    agent = CodingSession(
        provider=provider,
        sessions=store,
        events=event_bus,
        tool_context=ToolContext(cwd=tmp_path),
        skill_catalog=SkillCatalog(entries=(entry,)),
    )

    async def queue_invocation(event: object) -> None:
        if getattr(event, "type", None) == "agent.started":
            update = await agent.follow_up("/skill:demo finish this")
            assert update.follow_up == ("/skill:demo finish this",)

    event_bus.on("agent.started", queue_invocation)

    async def scenario() -> list[object]:
        return [event async for event in agent.run("start", session=session)]

    events = anyio.run(scenario)

    injected = next(event for event in events if isinstance(event, QueueMessageInjected))
    assert injected.kind == "follow_up"
    assert injected.skill_invocation is not None
    assert injected.skill_invocation.original_content == "/skill:demo finish this"
    assert injected.content.startswith("[WISP EXPLICIT SKILL]\nSkill: demo")
    queued_invocation = next(
        event
        for event in events
        if isinstance(event, SkillInvoked) and event.queue_kind == "follow_up"
    )
    assert queued_invocation.provider_content == injected.content
    assert provider.calls[1].messages[-1].content == injected.content
    persisted = session.read_context_messages()[-2]
    assert persisted.content == injected.content
    assert persisted.skill_invocation == injected.skill_invocation
