"""Assemble ordered provider-facing instructions for one turn."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from wisp.config.settings import DEFAULT_PROTECTED_PATHS
from wisp.events import DEFAULT_AGENT_MODE, AgentMode
from wisp.messages import Message
from wisp.providers.base import ToolSpec
from wisp.tools.base import ToolPromptMetadata

from .instructions import (
    DEFAULT_SYSTEM_PROMPT,
    INSTRUCTION_BOUNDARY_SYSTEM_PROMPT,
    PLAN_MODE_SYSTEM_PROMPT,
)
from .parts import AssembledPrompt, PromptPart
from .project_context import (
    DEFAULT_CONTEXT_FILE_MAX_CHARS,
    DEFAULT_CONTEXT_MAX_CHARS,
    assemble_project_context,
    assemble_untrusted_project_context,
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
    repository_status: str | None = None,
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
        repository_status (str | None): Git status snapshot to reuse; None reads Git.

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

    return assemble_prompt(
        cwd=cwd,
        tools=tools,
        tool_prompt_metadata=tool_prompt_metadata,
        additional_guidance=_host_guidance(additional_guidance),
        mode=mode,
        max_context_chars=max_context_chars,
        max_context_file_chars=max_context_file_chars,
        include_project_context=include_project_context,
        protected_paths=protected_paths,
        trusted_context_root=trusted_context_root,
        repository_status=repository_status,
    ).messages


def assemble_prompt(
    *,
    cwd: Path,
    tools: Sequence[ToolSpec] = (),
    tool_prompt_metadata: Sequence[ToolPromptMetadata] = (),
    additional_guidance: Sequence[PromptPart] = (),
    mode: AgentMode = DEFAULT_AGENT_MODE,
    max_context_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
    max_context_file_chars: int = DEFAULT_CONTEXT_FILE_MAX_CHARS,
    include_project_context: bool = True,
    protected_paths: tuple[str, ...] = DEFAULT_PROTECTED_PATHS,
    trusted_context_root: Path | None = None,
    repository_status: str | None = None,
) -> AssembledPrompt:
    """Assemble the prompt `build_prompt_messages` returns, with each message's parts.

    Takes the same arguments as `build_prompt_messages`, except that each piece of
    `additional_guidance` is a `PromptPart` naming the context source it counts
    toward, such as the skill index or web-search guidance.

    Returns:
        AssembledPrompt: The messages `build_prompt_messages` returns. Core
        instructions, tool guidance, the instruction boundary, and plan restrictions
        are system-prompt parts; the project-context message carries its own parts;
        each guidance message is its one `additional_guidance` part.

    Examples:
        >>> prompt = assemble_prompt(cwd=Path("."), include_project_context=False)
        >>> [[part.label for part in parts] for parts in prompt.parts]
        [['core instructions'], ['cwd', 'tool summary'], ['instruction boundary']]
    """

    context, context_parts = (
        assemble_project_context(
            cwd=cwd,
            tools=tools,
            max_chars=max_context_chars,
            max_context_file_chars=max_context_file_chars,
            trusted_context_root=trusted_context_root,
            protected_paths=protected_paths,
            repository_status=repository_status,
        )
        if include_project_context
        else assemble_untrusted_project_context(tools=tools, max_chars=max_context_chars)
    )
    messages = [
        (
            Message(
                role="system",
                content=DEFAULT_SYSTEM_PROMPT,
                prompt_cache_boundary=True,
            ),
            (PromptPart("system_prompt", "core instructions", DEFAULT_SYSTEM_PROMPT),),
        ),
        (Message(role="system", content=context), context_parts),
    ]
    if tool_guidance := _tool_guidance(tool_prompt_metadata):
        messages.append(
            (
                Message(role="system", content=tool_guidance),
                (PromptPart("system_prompt", "tool guidance", tool_guidance),),
            )
        )
    messages.extend(_guidance_messages(additional_guidance))
    messages.append(
        (
            Message(role="system", content=INSTRUCTION_BOUNDARY_SYSTEM_PROMPT),
            (
                PromptPart(
                    "system_prompt", "instruction boundary", INSTRUCTION_BOUNDARY_SYSTEM_PROMPT
                ),
            ),
        )
    )
    messages.extend(_mode_messages(mode))
    return _assembled(messages)


def extend_custom_prompt_messages(
    prompt_messages: Sequence[Message],
    *,
    additional_guidance: Sequence[str] = (),
    mode: AgentMode = DEFAULT_AGENT_MODE,
) -> tuple[Message, ...]:
    """Add the session's operational guidance to a caller-supplied prompt.

    A custom prompt replaces Wisp's instructions, project context, tool guidance,
    and instruction-boundary notice. The host guidance and plan-mode restrictions
    still apply, because they describe what the session currently allows; they
    follow the custom messages in the same order `build_prompt_messages` uses.

    Args:
        prompt_messages (Sequence[Message]): The caller's replacement prompt.
        additional_guidance (Sequence[str]): Host-provided guidance, in order.
        mode (AgentMode): Whether to append the plan-mode restrictions.

    Returns:
        tuple[Message, ...]: The custom messages, then non-blank host guidance,
        then the plan-mode restrictions in plan mode.

    Examples:
        >>> custom = (Message(role="system", content="Custom policy."),)
        >>> messages = extend_custom_prompt_messages(
        ...     custom, additional_guidance=("Skill index.", " "), mode="plan"
        ... )
        >>> [message.content for message in messages[:2]]
        ['Custom policy.', 'Skill index.']
        >>> messages[2].content == PLAN_MODE_SYSTEM_PROMPT
        True
    """

    return assemble_custom_prompt(
        prompt_messages, additional_guidance=_host_guidance(additional_guidance), mode=mode
    ).messages


def assemble_custom_prompt(
    prompt_messages: Sequence[Message],
    *,
    additional_guidance: Sequence[PromptPart] = (),
    mode: AgentMode = DEFAULT_AGENT_MODE,
) -> AssembledPrompt:
    """Extend a custom prompt as `extend_custom_prompt_messages` does, with its parts.

    Args:
        prompt_messages (Sequence[Message]): The caller's replacement prompt.
        additional_guidance (Sequence[PromptPart]): Host-provided guidance, in order,
            each naming the context source it counts toward.
        mode (AgentMode): Whether to append the plan-mode restrictions.

    Returns:
        AssembledPrompt: The messages `extend_custom_prompt_messages` returns. Each
        caller message is one custom-prompt part; guidance and plan restrictions are
        labelled as `assemble_prompt` labels them.
    """

    return _assembled(
        [
            *(
                (message, (PromptPart("custom_prompt", "custom prompt", message.content),))
                for message in prompt_messages
            ),
            *_guidance_messages(additional_guidance),
            *_mode_messages(mode),
        ]
    )


_LabelledMessage = tuple[Message, tuple[PromptPart, ...]]


def _assembled(messages: Sequence[_LabelledMessage]) -> AssembledPrompt:
    return AssembledPrompt(
        messages=tuple(message for message, _parts in messages),
        parts=tuple(parts for _message, parts in messages),
    )


def _host_guidance(guidance: Sequence[str]) -> tuple[PromptPart, ...]:
    """Label unlabelled guidance from callers that do not report a breakdown."""

    return tuple(PromptPart("system_prompt", "host guidance", text) for text in guidance)


def _guidance_messages(additional_guidance: Sequence[PromptPart]) -> tuple[_LabelledMessage, ...]:
    return tuple(
        (Message(role="system", content=guidance.text), (guidance,))
        for guidance in additional_guidance
        if guidance.text.strip()
    )


def _mode_messages(mode: AgentMode) -> tuple[_LabelledMessage, ...]:
    if mode == "plan":
        return (
            (
                Message(role="system", content=PLAN_MODE_SYSTEM_PROMPT),
                (PromptPart("system_prompt", "plan mode", PLAN_MODE_SYSTEM_PROMPT),),
            ),
        )
    return ()


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
