"""Tests for `wisp.providers`'s public package-level export surface.

Nothing internal to Wisp imports providers this way -- every internal call
site imports the specific submodule (`wisp.providers.openai`, etc.). These
package-level exports exist for external consumers embedding Wisp
(`from wisp.providers import AnthropicProvider`), so a missing export would
not be caught by any other test in this suite.
"""

from __future__ import annotations

import subprocess
import sys

import wisp.providers as providers_package
from wisp.providers import (
    AnthropicProvider,
    DeepSeekProvider,
    FakeProvider,
    GoogleProvider,
    OpenAICodexProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
    XAIProvider,
)


def test_all_built_in_providers_are_exported_from_the_package() -> None:
    assert AnthropicProvider.name == "anthropic"
    assert DeepSeekProvider.name == "deepseek"
    assert FakeProvider.name == "fake"
    assert GoogleProvider.name == "google"
    assert OpenAICodexProvider.name == "openai-codex"
    assert OpenAIProvider.name == "openai"
    assert OpenAICompatibleProvider.name == "openai-compatible"
    assert XAIProvider.name == "xai"


def test_all_built_in_provider_names_are_listed_in_dunder_all() -> None:
    assert {
        "AnthropicProvider",
        "DeepSeekProvider",
        "FakeProvider",
        "GoogleProvider",
        "OpenAICodexProvider",
        "OpenAIProvider",
        "OpenAICompatibleProvider",
        "XAIProvider",
    } <= set(providers_package.__all__)


def test_importing_the_cli_does_not_load_provider_sdks() -> None:
    """Importing Wisp must not pay for every vendor SDK.

    Each provider module imports its SDK at module scope, and those imports
    dominate cold start (~1.2 s) even though a run selects at most one provider.
    A subprocess is required because the SDKs may already be imported by an
    earlier test in this process.
    """

    program = (
        "import sys; import wisp.cli; "
        "print(','.join(m for m in ('google.genai', 'anthropic', 'openai') "
        "if m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == ""


def test_importing_the_cli_does_not_load_the_mcp_sdk() -> None:
    """MCP is optional, and its SDK costs ~430 ms to import.

    `McpRuntime` is only reachable when servers are configured, so importing the
    package must not pull it in. A subprocess is required because another test in
    this process may already have imported `mcp`.
    """

    program = "import sys; import wisp.cli; print('mcp' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "False"


def test_building_a_runtime_without_mcp_servers_does_not_load_the_mcp_sdk() -> None:
    """Registration must stay lazy through runtime construction, not just import."""

    program = (
        "import sys, anyio\n"
        "from wisp.runtime.extensions import build_runtime\n"
        "async def main():\n"
        "    await build_runtime()\n"
        "    print('mcp' in sys.modules)\n"
        "anyio.run(main)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "False"


def test_building_a_runtime_does_not_construct_providers() -> None:
    """Lazy registration must survive runtime construction, not just import.

    `build_runtime` previously snapshotted provider *instances* for configuration
    ownership, which constructed all seven and undid the deferral for every real
    command.
    """

    program = (
        "import sys, anyio\n"
        "from wisp.runtime.extensions import build_runtime\n"
        "async def main():\n"
        "    runtime = await build_runtime()\n"
        "    loaded = [m for m in ('google.genai', 'anthropic', 'openai') "
        "if m in sys.modules]\n"
        "    print(len(runtime.providers.names()), loaded)\n"
        "anyio.run(main)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=True,
    )

    count, loaded = result.stdout.strip().split(" ", 1)
    assert int(count) == 7
    assert loaded == "[]"


def test_provider_classes_remain_importable_from_the_package() -> None:
    """Deferring the imports must not remove the embedder-facing export surface."""

    program = (
        "from wisp.providers import AnthropicProvider, GoogleProvider, OpenAIProvider; "
        "print(AnthropicProvider.name, GoogleProvider.name, OpenAIProvider.name)"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.split() == ["anthropic", "google", "openai"]
