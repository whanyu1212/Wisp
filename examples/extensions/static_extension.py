"""Deterministic static extension example for Python embedders."""

from __future__ import annotations

from dataclasses import dataclass, field

import anyio

from wisp.events import WispEvent
from wisp.runtime import CommandDescriptor, ExtensionAPI, ToolPromptMetadata
from wisp.tools import (
    ToolArguments,
    ToolContext,
    ToolError,
    ToolInputSchema,
    ToolResult,
    ToolSafety,
)


@dataclass(slots=True)
class ExampleState:
    """Observable state owned by the embedding application."""

    event_types: list[str] = field(default_factory=list)


class GreetingTool:
    """Small read-only tool registered by the example extension."""

    name = "example_greeting"
    description = "Return a deterministic greeting for a supplied name."
    safety: ToolSafety = "read"
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name to greet"},
        },
        "required": ["name"],
        "additionalProperties": False,
    }

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        del context
        name = arguments.get("name")
        if type(name) is not str or not name.strip():
            raise ToolError("name must be a non-empty string")
        return ToolResult(text=f"Hello, {name.strip()}!")


def activate(api: ExtensionAPI, *, state: ExampleState | None = None) -> None:
    """Synchronously register the example's frontend-neutral capabilities."""

    selected_state = state if state is not None else ExampleState()

    def record_event(event: WispEvent) -> None:
        selected_state.event_types.append(event.type)

    api.register_tool(
        GreetingTool(),
        prompt=ToolPromptMetadata(
            prompt_snippet="Use example_greeting only when the user asks for a greeting."
        ),
        replace=False,
    )
    api.register_command(
        CommandDescriptor(
            name="example-status",
            title="Example status",
            description="Describe the installed static extension example.",
        )
    )
    api.on("agent.started", record_event)


async def activate_async(api: ExtensionAPI, *, state: ExampleState | None = None) -> None:
    """Asynchronous variant for extensions that need async setup."""

    await anyio.lowlevel.checkpoint()
    activate(api, state=state)


__all__ = ["ExampleState", "GreetingTool", "activate", "activate_async"]
