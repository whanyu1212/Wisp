"""Default Wisp capabilities registered through the extension API."""

from __future__ import annotations

from wisp.providers.fake import FakeProvider
from wisp.providers.openai import OpenAIProvider
from wisp.runtime.api import ExtensionAPI
from wisp.tools.builtin import builtin_tools


def activate(api: ExtensionAPI) -> None:
    """Register Wisp's baseline capabilities."""

    api.register_provider(FakeProvider())
    api.register_provider(OpenAIProvider())
    for tool in builtin_tools():
        api.register_tool(tool)
