from __future__ import annotations

from pathlib import Path

import anyio
import pytest
from textual.widgets import Input, OptionList

from wisp.auth.storage import JsonAuthStore
from wisp.tui.auth_commands import AuthCommands
from wisp.tui.connections import ConnectionMethodStatus, ConnectionProviderStatus
from wisp.tui.textual_app import create_textual_tui

pytestmark = pytest.mark.tui

_CONNECTIONS = (
    ConnectionProviderStatus(
        id="openai",
        label="OpenAI",
        methods=(
            ConnectionMethodStatus(
                provider="openai-codex",
                label="ChatGPT Plus/Pro",
                kind="device_code",
                source="stored",
            ),
            ConnectionMethodStatus(
                provider="openai",
                label="OpenAI API key",
                kind="api_key",
                source="missing",
                environment_variable="OPENAI_API_KEY",
            ),
        ),
    ),
    ConnectionProviderStatus(
        id="anthropic",
        label="Anthropic",
        methods=(
            ConnectionMethodStatus(
                provider="anthropic",
                label="Anthropic API key",
                kind="api_key",
                source="environment",
                environment_variable="ANTHROPIC_API_KEY",
            ),
        ),
    ),
)


def test_connect_panel_lists_provider_statuses() -> None:
    async def scenario() -> tuple[list[str], bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            renderer.connect_picker_request(_CONNECTIONS)
            await pilot.pause()
            options = app.query_one("#connect-options", OptionList)
            labels = [
                str(options.get_option_at_index(index).prompt)
                for index in range(options.option_count)
            ]
            api_key = app.query_one("#connect-api-key", Input)
            return labels, api_key.password

    labels, password = anyio.run(scenario)
    assert labels == ["OpenAI · 1 connected", "Anthropic · 1 connected"]
    assert password is True


def test_connect_panel_submits_oauth_through_direct_hook() -> None:
    async def scenario() -> list[str]:
        app, renderer = create_textual_tui()
        connected: list[str] = []

        async def connect(provider: str) -> None:
            connected.append(provider)
            renderer.connect_completed(provider)

        renderer.set_connect_oauth_hook(connect)
        async with app.run_test(size=(80, 24)) as pilot:
            renderer.connect_picker_request(_CONNECTIONS)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            return connected

    assert anyio.run(scenario) == ["openai-codex"]


def test_connect_panel_api_key_uses_redacted_callback() -> None:
    secret = "sentinel-secret-api-key"

    async def scenario() -> tuple[list[tuple[str, str]], str, int]:
        app, renderer = create_textual_tui()
        received: list[tuple[str, str]] = []

        async def save(provider: str, api_key: str) -> None:
            received.append((provider, api_key))
            renderer.connect_completed(provider)

        renderer.set_connect_api_key_hook(save)
        async with app.run_test(size=(80, 24)) as pilot:
            renderer.connect_picker_request(_CONNECTIONS, provider="openai")
            await pilot.pause()
            api_key = app.query_one("#connect-api-key", Input)
            assert api_key.display
            api_key.focus()
            await pilot.press(*secret)
            await pilot.press("enter")
            await pilot.pause()
            rendered = str(app.screen.render())
            queued = app._input_controller.receive_stream.statistics().current_buffer_used
            return received, rendered, queued

    received, rendered, queued = anyio.run(scenario)
    assert received == [("openai", secret)]
    assert secret not in rendered
    assert queued == 0


def test_explicit_connect_command_escape_cancels_device_authorization(tmp_path: Path) -> None:
    async def scenario() -> bool:
        app, renderer = create_textual_tui()
        commands = AuthCommands(
            renderer,
            lambda: JsonAuthStore(tmp_path / "auth.json"),
            lambda: "openai-codex",
        )
        started = anyio.Event()
        cancelled = anyio.Event()

        async def connect(_provider: str) -> None:
            started.set()
            try:
                await anyio.sleep_forever()
            finally:
                cancelled.set()

        renderer.set_connect_oauth_hook(connect)
        async with app.run_test(size=(80, 24)) as pilot:
            await commands.connect(("openai-codex",))
            await started.wait()
            await pilot.press("escape")
            with anyio.fail_after(1):
                await cancelled.wait()
            return app.query_one("#connect-panel").display

    assert anyio.run(scenario) is False


def test_disconnect_panel_submits_selected_provider() -> None:
    async def scenario() -> str:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            renderer.disconnect_picker_request(_CONNECTIONS)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            with anyio.fail_after(1):
                return await app._input_controller.receive_stream.receive()

    assert anyio.run(scenario) == "/disconnect openai-codex"
