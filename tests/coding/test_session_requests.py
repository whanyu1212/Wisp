from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import anyio

from tests.coding.session_support import (
    CapturingProvider,
)
from wisp.agent.messages import Message
from wisp.coding.session import CodingSession, _prompt_cache_key
from wisp.events import (
    MessageCompleted,
    ToolCallSnapshot,
)
from wisp.providers.base import (
    ToolSpec,
)
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.context import ToolContext


def test_continues_with_history_and_labeled_tool_observations(
    tmp_path: Path,
) -> None:
    provider = CapturingProvider()
    session = JsonlSessionStore(tmp_path).create()
    history = [
        Message(role="system", content="old instructions"),
        Message(role="user", content="previous question"),
        Message(
            role="assistant",
            content="",
            tool_calls=(
                ToolCallSnapshot(
                    call_id="call-1",
                    name="read",
                    arguments={"path": "README.md"},
                ),
            ),
            response_id="response-1",
            finish_reason="tool_calls",
        ),
        Message(
            role="tool",
            content="raw tool output must not be replayed as user text",
            tool_call_id="call-1",
            tool_name="read",
        ),
        Message(role="assistant", content="previous answer"),
    ]

    async def run_agent() -> list[object]:
        agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path))
        return [
            event async for event in agent.run("next question", session=session, history=history)
        ]

    anyio.run(run_agent)

    assert provider.seen_messages is not None
    assert [message.role for message in provider.seen_messages] == [
        "system",
        "system",
        "system",
        "user",
        "assistant",
        "assistant",
        "user",
    ]
    assert "You are Wisp" in provider.seen_messages[0].content
    assert provider.seen_messages[1].content.startswith("[WISP PROJECT CONTEXT]")
    assert provider.seen_messages[2].content.startswith("[WISP TRUST BOUNDARY]")
    assert provider.seen_messages[3].content == "previous question"
    payload = json.loads(provider.seen_messages[4].content)
    assert payload["type"] == "wisp.portable_tool_exchange"
    assert payload["calls"][0]["arguments"] == {"path": "README.md"}
    assert payload["calls"][0]["result"]["output"] == (
        "raw tool output must not be replayed as user text"
    )
    assert [message.content for message in provider.seen_messages[5:]] == [
        "previous answer",
        "next question",
    ]
    assert not any(
        message.role == "assistant" and not message.content for message in provider.seen_messages
    )

    records = [json.loads(line) for line in session.path.read_text(encoding="utf-8").splitlines()]
    assert [record["message"]["role"] for record in records] == [
        "system",
        "system",
        "system",
        "user",
        "assistant",
    ]
    assert records[3]["message"]["content"] == "next question"
    assert records[4]["message"]["content"] == "done"


def test_passes_tool_specs_to_provider(tmp_path: Path) -> None:
    provider = CapturingProvider()
    tool = ToolSpec(
        name="lookup",
        description="Look something up.",
        input_schema={"type": "object", "properties": {}},
    )

    async def run_agent() -> list[object]:
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tools=[tool],
        )
        return [event async for event in agent.run("hello")]

    events = anyio.run(run_agent)

    assert provider.seen_messages is not None
    assert [message.role for message in provider.seen_messages] == [
        "system",
        "system",
        "system",
        "user",
    ]
    assert "You are Wisp" in provider.seen_messages[0].content
    assert "allowed tools:\n  - lookup: Look something up." in provider.seen_messages[1].content
    assert provider.seen_messages[2].content.startswith("[WISP TRUST BOUNDARY]")
    assert provider.seen_messages[3].content == "hello"
    assert provider.seen_tools == (tool,)
    assert any(isinstance(event, MessageCompleted) and event.content == "done" for event in events)


def test_custom_prompt_messages_remain_full_replacement(tmp_path: Path) -> None:
    provider = CapturingProvider()
    custom_prompt = Message(role="system", content="Custom application policy.")

    async def run_agent() -> None:
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            prompt_messages=(custom_prompt,),
        )
        _ = [event async for event in agent.run("hello")]

    anyio.run(run_agent)

    assert provider.seen_messages is not None
    assert [(message.role, message.content) for message in provider.seen_messages] == [
        ("system", "Custom application policy."),
        ("user", "hello"),
    ]


def test_passes_effort_to_provider(tmp_path: Path) -> None:
    provider = CapturingProvider()

    async def run_agent() -> list[object]:
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            effort="high",
        )
        return [event async for event in agent.run("hello")]

    anyio.run(run_agent)

    assert provider.seen_effort == "high"


def test_defaults_effort_to_none(tmp_path: Path) -> None:
    provider = CapturingProvider()

    async def run_agent() -> list[object]:
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
        )
        return [event async for event in agent.run("hello")]

    anyio.run(run_agent)

    assert provider.seen_effort is None


def test_reuses_cache_key_for_session_and_isolates_sessions(
    tmp_path: Path,
) -> None:
    provider = CapturingProvider()
    sessions = JsonlSessionStore(tmp_path)
    first = sessions.create()
    second = sessions.create()

    async def run_agent() -> None:
        agent = CodingSession(provider=provider, sessions=sessions)
        _ = [event async for event in agent.run("first", session=first)]
        _ = [
            event
            async for event in agent.run(
                "resume",
                session=first,
                history=first.read_context_messages(),
            )
        ]
        _ = [event async for event in agent.run("other", session=second)]

    anyio.run(run_agent)

    assert provider.seen_prompt_cache_keys == [
        _prompt_cache_key(first.session_id),
        _prompt_cache_key(first.session_id),
        _prompt_cache_key(second.session_id),
    ]
    assert provider.seen_prompt_cache_keys[0] != provider.seen_prompt_cache_keys[2]


def test_skips_project_context_when_untrusted(tmp_path: Path) -> None:
    provider = CapturingProvider()
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (project / "AGENTS.md").write_text("Never show untrusted agent rules.\n", encoding="utf-8")
    (project / "CLAUDE.md").write_text("Never show untrusted Claude rules.\n", encoding="utf-8")
    tool = ToolSpec(
        name="lookup",
        description="Look something up.",
        input_schema={"type": "object", "properties": {}},
    )

    async def run_agent() -> list[object]:
        agent = CodingSession(
            provider=cast(Any, provider),
            sessions=JsonlSessionStore(tmp_path),
            tools=[tool],
            tool_context=ToolContext(cwd=project),
            trusted=False,
        )
        return [event async for event in agent.run("hello")]

    anyio.run(run_agent)

    assert provider.seen_messages is not None
    context = provider.seen_messages[1].content
    assert "project context: skipped because this project is not trusted" in context
    assert str(project.resolve(strict=False)) not in context
    assert "pyproject.toml" not in context
    assert "AGENTS.md" not in context
    assert "CLAUDE.md" not in context
    assert "Never show untrusted agent rules." not in context
    assert "Never show untrusted Claude rules." not in context
    assert "allowed tools:\n  - lookup: Look something up." in context


def test_includes_project_context_when_trusted(tmp_path: Path) -> None:
    provider = CapturingProvider()
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (project / "AGENTS.md").write_text("Trusted agent rules.\n", encoding="utf-8")

    async def run_agent() -> list[object]:
        agent = CodingSession(
            provider=cast(Any, provider),
            sessions=JsonlSessionStore(tmp_path),
            tool_context=ToolContext(cwd=project),
            trusted=True,
        )
        return [event async for event in agent.run("hello")]

    anyio.run(run_agent)

    assert provider.seen_messages is not None
    context = provider.seen_messages[1].content
    assert f"cwd: {project.resolve(strict=False)}" in context
    assert "project files:\n  pyproject.toml" in context
    assert "--- AGENTS.md ---\nTrusted agent rules." in context
