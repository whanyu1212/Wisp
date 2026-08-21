"""Default Wisp capabilities registered through the extension API."""

from __future__ import annotations

from wisp.auth.storage import JsonAuthStore
from wisp.openai_compatible import OpenAICompatibleSettings
from wisp.providers.auth import StoredProviderAuthResolver
from wisp.providers.base import Provider
from wisp.providers.fake import FakeProvider
from wisp.retry import RetryPolicy
from wisp.runtime.api import ExtensionAPI
from wisp.runtime.builtin_commands import builtin_command_descriptors
from wisp.skills.tool import SkillTool
from wisp.tools.base import ToolExecutionMetadata, ToolPromptMetadata
from wisp.tools.builtin import builtin_tools
from wisp.tools.process_manager import ProcessSupervisor

_SEARCH_GUIDELINE = "Prefer the dedicated read-only search tools over bash when they fit."
_PARALLEL_TOOL_EXECUTION = ToolExecutionMetadata(parallel_safe=True)
_PARALLEL_SAFE_TOOL_NAMES = frozenset({"find", "grep", "ls", "read"})
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
    openai_compatible: OpenAICompatibleSettings | None = None,
) -> None:
    """Register Wisp's baseline capabilities."""

    auth_resolver = StoredProviderAuthResolver(auth_store) if auth_store is not None else None
    # Every provider below imports a vendor SDK at module scope, and those imports
    # dominate cold start (~1.4 s for the set) even though a run selects at most one.
    # Registering factories keeps the names available while deferring each import to
    # the first request for that provider. `FakeProvider` has no SDK, so it stays
    # eager and keeps the offline path free of indirection.
    api.register_provider(FakeProvider())

    def _openai() -> Provider:
        from wisp.providers.openai import OpenAIProvider

        return OpenAIProvider(auth_resolver=auth_resolver, retry_policy=retry_policy)

    def _xai() -> Provider:
        from wisp.providers.xai import XAIProvider

        return XAIProvider(auth_resolver=auth_resolver, retry_policy=retry_policy)

    def _deepseek() -> Provider:
        from wisp.providers.deepseek import DeepSeekProvider

        return DeepSeekProvider(auth_resolver=auth_resolver, retry_policy=retry_policy)

    def _openai_codex() -> Provider:
        from wisp.providers.openai_codex import OpenAICodexProvider

        return OpenAICodexProvider(auth_resolver=auth_resolver, retry_policy=retry_policy)

    def _anthropic() -> Provider:
        from wisp.providers.anthropic import AnthropicProvider

        return AnthropicProvider(auth_resolver=auth_resolver, retry_policy=retry_policy)

    def _google() -> Provider:
        from wisp.providers.google import GoogleProvider

        return GoogleProvider(auth_resolver=auth_resolver, retry_policy=retry_policy)

    api.register_provider_factory("openai", _openai)
    api.register_provider_factory("xai", _xai)
    api.register_provider_factory("deepseek", _deepseek)
    if openai_compatible is not None:
        settings = openai_compatible

        def _openai_compatible() -> Provider:
            from wisp.providers.openai_compatible import OpenAICompatibleProvider

            return OpenAICompatibleProvider(
                provider_name=settings.provider_name,
                base_url=settings.base_url,
                default_model=settings.default_model,
                requires_api_key=settings.requires_api_key,
                ca_bundle=settings.ca_bundle,
                auth_resolver=auth_resolver,
                retry_policy=retry_policy,
            )

        api.register_provider_factory(settings.provider_name, _openai_compatible)
    api.register_provider_factory("openai-codex", _openai_codex)
    api.register_provider_factory("anthropic", _anthropic)
    api.register_provider_factory("google", _google)
    for tool in builtin_tools(process_supervisor=process_supervisor):
        api.register_tool(
            tool,
            execution=(
                _PARALLEL_TOOL_EXECUTION if tool.name in _PARALLEL_SAFE_TOOL_NAMES else None
            ),
            prompt=_BUILTIN_TOOL_PROMPTS.get(tool.name),
        )
    api.register_tool(SkillTool(), execution=_PARALLEL_TOOL_EXECUTION)
    for command in builtin_command_descriptors():
        api.register_command(command)
