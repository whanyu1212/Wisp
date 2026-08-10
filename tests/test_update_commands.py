from __future__ import annotations

from typing import cast

import anyio

from wisp.tui.rendering import TuiRenderer
from wisp.tui.update_commands import UpdateCommands
from wisp.update_check import UpdateAvailable, UpdateCheckError, UpdateStatus


class RecordingRenderer:
    def __init__(self) -> None:
        self.notices: list[str] = []
        self.errors: list[str] = []

    def notice(self, message: str) -> None:
        self.notices.append(message)

    def command_error(self, message: str) -> None:
        self.errors.append(message)


def test_tui_update_check_then_explicit_install() -> None:
    renderer = RecordingRenderer()
    installed: list[UpdateAvailable] = []

    async def check() -> UpdateStatus:
        return UpdateStatus("1.0.0", "1.2.0")

    async def install(update: UpdateAvailable) -> None:
        installed.append(update)

    commands = UpdateCommands(
        cast(TuiRenderer, renderer),
        checker=check,
        installer=install,
    )

    anyio.run(commands.run, ())
    anyio.run(commands.run, ("install",))

    assert renderer.notices == [
        "Wisp 1.2.0 is available (current 1.0.0). Run /update install to install it.",
        "Updated Wisp to 1.2.0. Restart Wisp to use the new version.",
    ]
    assert [update.latest_version for update in installed] == ["1.2.0"]


def test_tui_update_reports_errors_and_usage() -> None:
    renderer = RecordingRenderer()

    async def fail() -> UpdateStatus:
        raise UpdateCheckError("offline")

    commands = UpdateCommands(cast(TuiRenderer, renderer), checker=fail)

    anyio.run(commands.run, ("later",))
    anyio.run(commands.run, ())

    assert renderer.errors == [
        "Usage: /update [check|install]",
        "Update check failed: offline",
    ]
