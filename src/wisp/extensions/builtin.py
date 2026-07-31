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
from wisp.tools.builtin import builtin_tools
from wisp.tools.process_manager import ProcessSupervisor


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
        api.register_tool(tool)
    for command in builtin_command_descriptors():
        api.register_command(command)
