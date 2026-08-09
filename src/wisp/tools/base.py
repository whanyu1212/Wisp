"""Tool protocol for local capabilities."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from wisp.tool_types import ToolSafety as ToolSafety
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolResult

ToolArguments = Mapping[str, object]
ToolInputSchema = Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ToolPromptMetadata:
    """Optional model guidance kept separate from a tool's provider schema."""

    prompt_snippet: str | None = None
    guidelines: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.prompt_snippet is not None and type(self.prompt_snippet) is not str:
            raise TypeError("Tool prompt snippet must be a string")
        guidelines = tuple(self.guidelines)
        if any(type(guideline) is not str for guideline in guidelines):
            raise TypeError("Tool prompt guidelines must be strings")
        object.__setattr__(self, "guidelines", guidelines)


class Tool(Protocol):
    """Protocol implemented by tools registered with the runtime."""

    name: str
    description: str
    input_schema: ToolInputSchema
    safety: ToolSafety

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        """Execute the tool with JSON-like arguments."""
        ...
