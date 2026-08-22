"""Provider implementations."""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Import-time cost is what this module defers, and TYPE_CHECKING blocks never
    # run. Declaring the lazy classes here keeps `from wisp.providers import
    # OpenAIProvider` a concrete type for the typed embedders this package
    # advertises via py.typed, which a bare `__getattr__` would erase to `object`.
    from .anthropic import AnthropicProvider
    from .deepseek import DeepSeekProvider
    from .google import GoogleProvider
    from .openai import OpenAIProvider
    from .openai_codex import OpenAICodexProvider
    from .openai_compatible import OpenAICompatibleProvider
    from .xai import XAIProvider

from wisp.retry import RetryPolicy, RetrySettings

from .base import ContextOverflowError, ToolCallResult, ToolSpec
from .events import (
    ProviderEvent,
    ProviderFailureKind,
    ProviderFinishReason,
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderRetrying,
    ProviderTextDelta,
    ProviderThinkingDelta,
    ProviderToolCallCompleted,
    ToolCall,
)
from .fake import FakeProvider, ProviderRequest, ScriptedProvider

# Provider classes that import a vendor SDK are resolved on attribute access rather
# than at package import. Importing any submodule runs this file first, so eager
# imports here cost every process ~1.4 s of SDK loading even when it never selects a
# provider. PEP 562 keeps `from wisp.providers import AnthropicProvider` working for
# embedders while charging the import only to callers that ask for it.
_LAZY_PROVIDERS = {
    "AnthropicProvider": ".anthropic",
    "DeepSeekProvider": ".deepseek",
    "GoogleProvider": ".google",
    "OpenAICodexProvider": ".openai_codex",
    "OpenAICompatibleProvider": ".openai_compatible",
    "OpenAIProvider": ".openai",
    "XAIProvider": ".xai",
}


def __getattr__(name: str) -> object:
    module_name = _LAZY_PROVIDERS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(module_name, __name__)
    attribute = getattr(module, name)
    globals()[name] = attribute
    return attribute


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "AnthropicProvider",
    "ContextOverflowError",
    "DeepSeekProvider",
    "FakeProvider",
    "GoogleProvider",
    "OpenAICodexProvider",
    "OpenAIProvider",
    "OpenAICompatibleProvider",
    "XAIProvider",
    "ProviderEvent",
    "ProviderFailureKind",
    "ProviderFinishReason",
    "ProviderRequest",
    "ProviderResponseCompleted",
    "ProviderResponseFailed",
    "ProviderResponseStarted",
    "ProviderRetrying",
    "ProviderTextDelta",
    "ProviderThinkingDelta",
    "ProviderToolCallCompleted",
    "ScriptedProvider",
    "RetryPolicy",
    "RetrySettings",
    "ToolCall",
    "ToolCallResult",
    "ToolSpec",
]
