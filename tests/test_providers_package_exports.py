"""Tests for `wisp.providers`'s public package-level export surface.

Nothing internal to Wisp imports providers this way -- every internal call
site imports the specific submodule (`wisp.providers.openai`, etc.). These
package-level exports exist for external consumers embedding Wisp
(`from wisp.providers import AnthropicProvider`), so a missing export would
not be caught by any other test in this suite.
"""

from __future__ import annotations

import wisp.providers as providers_package
from wisp.providers import (
    AnthropicProvider,
    FakeProvider,
    GoogleProvider,
    OpenAICodexProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
    XAIProvider,
)


def test_all_built_in_providers_are_exported_from_the_package() -> None:
    assert AnthropicProvider.name == "anthropic"
    assert FakeProvider.name == "fake"
    assert GoogleProvider.name == "google"
    assert OpenAICodexProvider.name == "openai-codex"
    assert OpenAIProvider.name == "openai"
    assert OpenAICompatibleProvider.name == "openai-compatible"
    assert XAIProvider.name == "xai"


def test_all_built_in_provider_names_are_listed_in_dunder_all() -> None:
    assert {
        "AnthropicProvider",
        "FakeProvider",
        "GoogleProvider",
        "OpenAICodexProvider",
        "OpenAIProvider",
        "OpenAICompatibleProvider",
        "XAIProvider",
    } <= set(providers_package.__all__)
