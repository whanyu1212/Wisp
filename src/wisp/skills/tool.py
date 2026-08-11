"""Read-only tool for progressively loading Agent Skills resources."""

from __future__ import annotations

from functools import partial

import anyio

from wisp.skills.loading import load_skill_resource
from wisp.skills.models import SkillCatalog
from wisp.skills.prompt import format_skill_content
from wisp.tools.base import ToolArguments, ToolInputSchema, ToolSafety
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.truncation import TruncatedText, truncate_text


class SkillTool:
    """Load instructions and supporting files from one immutable catalog."""

    name = "skill"
    safety: ToolSafety = "read"
    description = "Load instructions or a relative supporting resource from an available skill."
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name from the available skills index"},
            "resource": {
                "type": "string",
                "description": "Optional relative resource path; omit to load SKILL.md",
            },
        },
        "required": ["name"],
    }

    def __init__(self, catalog: SkillCatalog | None = None) -> None:
        self.catalog = catalog or SkillCatalog()

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        name = arguments.get("name")
        if type(name) is not str or not name:
            raise ToolError("skill.name must be a non-empty string")
        raw_resource = arguments.get("resource")
        if raw_resource is not None and type(raw_resource) is not str:
            raise ToolError("skill.resource must be a string")
        entry = self.catalog.get(name)
        if entry is None:
            available = ", ".join(self.catalog.names()) or "none"
            raise ToolError(f"Unknown skill {name!r}; available skills: {available}")
        resource = await anyio.to_thread.run_sync(
            partial(
                load_skill_resource,
                entry,
                raw_resource,
                context=context,
            ),
            abandon_on_cancel=True,
        )
        bounded = _bound_skill_content(
            format_skill_content(
                name=name,
                resource=resource.resource,
                content=resource.text,
            ),
            context=context,
        )
        return ToolResult(
            text=bounded.text,
            data={"skill": name, "resource": resource.resource},
            truncated=resource.truncated or bounded.truncated,
        )


def _bound_skill_content(text: str, *, context: ToolContext) -> TruncatedText:
    max_bytes = max(0, context.max_output_bytes)
    max_lines = max(0, context.max_output_lines)
    if max_lines == 0:
        return TruncatedText(text="", truncated=bool(text))

    bounded = truncate_text(text, max_bytes=max_bytes, max_lines=max_lines)
    if len(bounded.text.splitlines()) <= max_lines:
        return bounded
    return truncate_text(text, max_bytes=max_bytes, max_lines=max_lines - 1)


__all__ = ["SkillTool"]
