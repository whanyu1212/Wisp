"""Default Wisp capabilities registered through the extension API."""

from __future__ import annotations

from wisp.auth.storage import JsonAuthStore
from wisp.providers.anthropic import AnthropicProvider
from wisp.providers.auth import StoredProviderAuthResolver
from wisp.providers.fake import FakeProvider
from wisp.providers.google import GoogleProvider
from wisp.providers.openai import OpenAIProvider
from wisp.providers.openai_codex import OpenAICodexProvider
from wisp.retry import RetryPolicy
from wisp.runtime.api import ExtensionAPI
from wisp.runtime.builtin_commands import builtin_command_descriptors
from wisp.tools.base import ToolPromptMetadata
from wisp.tools.builtin import builtin_tools
from wisp.tools.process_manager import ProcessSupervisor

_SEARCH_GUIDELINE = "Prefer the dedicated read-only search tools over bash when they fit."
_BUILTIN_TOOL_PROMPTS = {
    "read": ToolPromptMetadata(
        prompt_snippet="Use read with offset and limit instead of loading a large file at once.",
    ),
    "write": ToolPromptMetadata(
        prompt_snippet="Use write for intentional whole-file creation or replacement.",
    ),
    "edit": ToolPromptMetadata(
        prompt_snippet="Use edit for exact, uniquely matched replacements in an existing file.",
    ),
    "bash": ToolPromptMetadata(
        prompt_snippet=(
            "Use bash for commands; use start, poll, and cancel for retained "
            "long-running processes."
        ),
        guidelines=(
            "Treat nonzero exit codes and timeouts as failures unless documented otherwise.",
        ),
    ),
    "grep": ToolPromptMetadata(guidelines=(_SEARCH_GUIDELINE,)),
    "find": ToolPromptMetadata(guidelines=(_SEARCH_GUIDELINE,)),
    "ls": ToolPromptMetadata(guidelines=(_SEARCH_GUIDELINE,)),
}


def activate(
    api: ExtensionAPI,
    *,
    auth_store: JsonAuthStore | None = None,
    retry_policy: RetryPolicy | None = None,
    process_supervisor: ProcessSupervisor | None = None,
) -> None:
    """Register Wisp's baseline capabilities."""

    api.register_provider(FakeProvider())
    api.register_provider(OpenAIProvider(retry_policy=retry_policy))
    api.register_provider(
        OpenAICodexProvider(
            auth_resolver=StoredProviderAuthResolver(auth_store)
            if auth_store is not None
            else None,
            retry_policy=retry_policy,
        )
    )
    api.register_provider(AnthropicProvider(retry_policy=retry_policy))
    api.register_provider(GoogleProvider(retry_policy=retry_policy))
    for tool in builtin_tools(process_supervisor=process_supervisor):
        api.register_tool(tool, prompt=_BUILTIN_TOOL_PROMPTS.get(tool.name))
    for command in builtin_command_descriptors():
        api.register_command(command)
