"""Executable coverage for the static extension authoring example."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from examples.extensions.static_extension import ExampleState, activate, activate_async
from wisp.agent.loop import AgentLoopConfig, run_agent_loop
from wisp.agent.messages import Message
from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import AgentStarted, ToolExecutionEnded
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderToolCallCompleted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider
from wisp.runtime import CommandRegistry, EventBus, ExtensionAPI, ProviderRegistry, ToolRegistry
from wisp.runtime.extensions import activate_extensions
from wisp.tools import ToolApprovalPolicy, ToolContext, ToolPolicy


def _api() -> tuple[ExtensionAPI, ToolRegistry, CommandRegistry, EventBus]:
    tools = ToolRegistry()
    commands = CommandRegistry()
    events = EventBus()
    return (
        ExtensionAPI(
            providers=ProviderRegistry(),
            tools=tools,
            commands=commands,
            events=events,
        ),
        tools,
        commands,
        events,
    )


def test_static_extension_registers_frontend_neutral_capabilities() -> None:
    api, tools, commands, _events = _api()

    activate(api)

    assert tools.names() == ("example_greeting",)
    assert tools.specs()[0].name == "example_greeting"
    assert tools.prompt_metadata(("example_greeting",))[0].prompt_snippet is not None
    descriptor = commands.get("example-status")
    assert descriptor.title == "Example status"


def test_static_extension_tool_executes_through_normal_policy(tmp_path: Path) -> None:
    api, tools, _commands, _events = _api()
    activate(api)
    executor = ConfiguredToolExecutor(
        registry=tools,
        context=ToolContext(cwd=tmp_path),
        policy=ToolPolicy.allow_read_tools(),
        approval_policy=ToolApprovalPolicy.require_approval(),
    )

    async def run() -> tuple[object, ...]:
        call = ToolCall(
            call_id="greeting-1",
            name="example_greeting",
            arguments={"name": "Wisp"},
        )
        return tuple([event async for event in executor.execute(call)])

    events = anyio.run(run)

    assert len(events) == 1
    ended = events[0]
    assert isinstance(ended, ToolExecutionEnded)
    assert ended.output == "Hello, Wisp!"
    assert ended.is_error is False


def test_static_extension_tool_is_provider_visible_and_returns_results(tmp_path: Path) -> None:
    api, tools, _commands, _events = _api()
    activate(api)
    call = ToolCall(
        call_id="greeting-1",
        name="example_greeting",
        arguments={"name": "Wisp"},
    )
    provider = ScriptedProvider(
        (
            (
                ProviderResponseStarted(model="scripted"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                ),
            ),
            (
                ProviderResponseStarted(model="scripted"),
                ProviderResponseCompleted(content="done"),
            ),
        )
    )
    executor = ConfiguredToolExecutor(
        registry=tools,
        context=ToolContext(cwd=tmp_path),
        policy=ToolPolicy.allow_read_tools(),
        approval_policy=ToolApprovalPolicy.require_approval(),
    )

    async def run() -> None:
        events = [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=executor,
                    tools=tools.specs(),
                ),
                messages=(Message(role="user", content="Greet Wisp"),),
            )
        ]
        assert any(isinstance(event, ToolExecutionEnded) for event in events)

    anyio.run(run)

    assert [spec.name for spec in provider.calls[0].tools] == ["example_greeting"]
    assert provider.calls[1].tool_results[0].output == "Hello, Wisp!"


def test_static_extension_event_handler_observes_typed_events() -> None:
    api, _tools, _commands, events = _api()
    state = ExampleState()
    activate(api, state=state)

    anyio.run(events.emit, AgentStarted(session_id="session-1"))

    assert state.event_types == ["agent.started"]


def test_async_static_extension_factory_is_awaited() -> None:
    api, tools, commands, _events = _api()
    state = ExampleState()

    async def extension(selected_api: ExtensionAPI) -> None:
        await activate_async(selected_api, state=state)

    anyio.run(activate_extensions, api, (extension,))

    assert tools.names() == ("example_greeting",)
    assert commands.names() == ("example-status",)


def test_duplicate_static_activation_fails_explicitly() -> None:
    api, _tools, _commands, _events = _api()
    activate(api)

    with pytest.raises(ValueError, match="already registered"):
        activate(api)
