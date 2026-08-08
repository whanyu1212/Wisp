"""Explicit Agent Skill prompt invocation and expansion."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import partial

import anyio

from wisp.skills.loading import load_skill_resource
from wisp.skills.models import SkillCatalog, SkillInvocationEvidence
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError

_SKILL_INVOCATION = re.compile(
    r"^/skill:(?P<name>[a-z0-9]+(?:-[a-z0-9]+)*)(?:(?:[ \t]+|\r?\n)(?P<request>[\s\S]*))?$"
)
_EXPANSION_HEADER = "[WISP EXPLICIT SKILL]"


@dataclass(frozen=True, slots=True)
class ParsedSkillInvocation:
    """One syntactically valid explicit invocation before resource loading."""

    name: str
    original_content: str
    request: str


def parse_skill_invocation(content: str) -> ParsedSkillInvocation | None:
    """Parse a complete leading ``/skill:<name>`` directive."""

    match = _SKILL_INVOCATION.fullmatch(content)
    if match is None:
        return None
    return ParsedSkillInvocation(
        name=match.group("name"),
        original_content=content,
        request=match.group("request") or "",
    )


async def expand_skill_invocation(
    content: str,
    *,
    catalog: SkillCatalog,
    context: ToolContext,
) -> tuple[str, SkillInvocationEvidence | None]:
    """Resolve and expand an explicit invocation from one catalog snapshot."""

    invocation = parse_skill_invocation(content)
    if invocation is None:
        return content, None
    entry = catalog.get(invocation.name)
    if entry is None:
        available = ", ".join(catalog.names()) or "none"
        raise ToolError(f"Unknown skill {invocation.name!r}; available skills: {available}")
    resource = await anyio.to_thread.run_sync(
        partial(load_skill_resource, entry, None, context=context),
        abandon_on_cancel=True,
    )
    content_sha256 = hashlib.sha256(resource.text.encode("utf-8")).hexdigest()
    evidence = SkillInvocationEvidence(
        name=invocation.name,
        original_content=invocation.original_content,
        request=invocation.request,
        content_sha256=content_sha256,
        instructions_truncated=resource.truncated,
    )
    request = invocation.request or "Apply these skill instructions to the current task."
    expanded = (
        f"{_EXPANSION_HEADER}\n"
        f"Skill: {invocation.name}\n"
        f"Content-SHA256: {content_sha256}\n\n"
        f"[SKILL INSTRUCTIONS]\n{resource.text}\n\n"
        f"[USER REQUEST]\n{request}"
    )
    return expanded, evidence


__all__ = [
    "ParsedSkillInvocation",
    "expand_skill_invocation",
    "parse_skill_invocation",
]
