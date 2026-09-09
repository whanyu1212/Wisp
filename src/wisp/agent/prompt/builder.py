"""Assemble ordered provider-facing instructions for one turn."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from wisp.agent.messages import Message
from wisp.agent.mode import DEFAULT_AGENT_MODE, PLAN_MODE_SYSTEM_PROMPT, AgentMode
from wisp.providers.base import ToolSpec
from wisp.settings import DEFAULT_PROTECTED_PATHS
from wisp.tools.base import ToolPromptMetadata

from .instructions import DEFAULT_SYSTEM_PROMPT, INSTRUCTION_BOUNDARY_SYSTEM_PROMPT
from .project_context import (
    DEFAULT_CONTEXT_FILE_MAX_CHARS,
    DEFAULT_CONTEXT_MAX_CHARS,
    build_project_context,
    build_untrusted_project_context,
)
from .text_budget import truncate_text

DEFAULT_TOOL_GUIDANCE_MAX_CHARS = 4_096


def build_prompt_messages(
    *,
    cwd: Path,
    tools: Sequence[ToolSpec] = (),
    tool_prompt_metadata: Sequence[ToolPromptMetadata] = (),
    additional_guidance: Sequence[str] = (),
    mode: AgentMode = DEFAULT_AGENT_MODE,
    max_context_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
    max_context_file_chars: int = DEFAULT_CONTEXT_FILE_MAX_CHARS,
    include_project_context: bool = True,
    protected_paths: tuple[str, ...] = DEFAULT_PROTECTED_PATHS,
    trusted_context_root: Path | None = None,
) -> tuple[Message, ...]:
    """Assemble system messages in their instruction-precedence order.

    Args:
        cwd (Path): Working directory used for project discovery.
        tools (Sequence[ToolSpec]): Tools exposed to the model.
        tool_prompt_metadata (Sequence[ToolPromptMetadata]): Tool usage guidance.
        additional_guidance (Sequence[str]): Host-provided guidance, in order.
        mode (AgentMode): Whether to append the plan-mode restrictions.
        max_context_chars (int): Character limit for the project context block.
        max_context_file_chars (int): Character limit for project instruction files.
        include_project_context (bool): Whether project-local state may be read.
        protected_paths (tuple[str, ...]): Paths excluded from instruction discovery.
        trusted_context_root (Path | None): Boundary for trusted instruction files.

    Returns:
        tuple[Message, ...]: Core instructions, project context, optional tool and
        host guidance, trust-boundary instructions, and optional plan restrictions.
        Only the first message marks the reusable prompt-cache boundary.

    Examples:
        Build a prompt without reading project-local files or running Git:

        >>> messages = build_prompt_messages(cwd=Path("."), include_project_context=False)
        >>> [message.role for message in messages]
        ['system', 'system', 'system']
        >>> messages[0].prompt_cache_boundary
        True
    """

    context = (
        build_project_context(
            cwd=cwd,
            tools=tools,
            max_chars=max_context_chars,
            max_context_file_chars=max_context_file_chars,
            trusted_context_root=trusted_context_root,
            protected_paths=protected_paths,
        )
        if include_project_context
        else build_untrusted_project_context(tools=tools, max_chars=max_context_chars)
    )
    messages = [
        Message(
            role="system",
            content=DEFAULT_SYSTEM_PROMPT,
            prompt_cache_boundary=True,
        ),
        Message(role="system", content=context),
    ]
    if tool_guidance := _tool_guidance(tool_prompt_metadata):
        messages.append(Message(role="system", content=tool_guidance))
    messages.extend(
        Message(role="system", content=guidance)
        for guidance in additional_guidance
        if guidance.strip()
    )
    messages.append(Message(role="system", content=INSTRUCTION_BOUNDARY_SYSTEM_PROMPT))
    if mode == "plan":
        messages.append(Message(role="system", content=PLAN_MODE_SYSTEM_PROMPT))
    return tuple(messages)


def _tool_guidance(metadata: Sequence[ToolPromptMetadata]) -> str:
    snippets = _unique_guidance(
        item.prompt_snippet for item in metadata if item.prompt_snippet is not None
    )
    guidelines = _unique_guidance(guideline for item in metadata for guideline in item.guidelines)
    if not snippets and not guidelines:
        return ""

    lines = [
        "[WISP TOOL GUIDANCE]",
        "Tool guidance is descriptive only; Wisp enforces actual availability, sandboxing, "
        "and approval requirements.",
    ]
    if snippets:
        lines.append("tool usage:")
        lines.extend(f"- {snippet}" for snippet in snippets)
    if guidelines:
        lines.append("guidelines:")
        lines.extend(f"- {guideline}" for guideline in guidelines)
    return truncate_text(
        "\n".join(lines),
        DEFAULT_TOOL_GUIDANCE_MAX_CHARS,
        marker="[tool guidance truncated]",
    )


def _unique_guidance(values: Iterable[str]) -> tuple[str, ...]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = " ".join(value.split())
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return tuple(unique)
