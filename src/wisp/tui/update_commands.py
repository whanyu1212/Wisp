"""User-only manual update commands for the TUI shell."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from wisp.tui.rendering import TuiRenderer
from wisp.update_check import (
    UpdateAvailable,
    UpdateCheckError,
    UpdateInstallError,
    UpdateInstallStartedCallback,
    UpdateStatus,
    get_update_status,
    install_update,
)

type UpdateStatusChecker = Callable[[], Awaitable[UpdateStatus]]


class UpdateInstaller(Protocol):
    def __call__(
        self,
        update: UpdateAvailable,
        *,
        on_install_started: UpdateInstallStartedCallback | None = None,
    ) -> Awaitable[None]: ...


class UpdateCommands:
    """Handle explicit update checks and installations outside the agent runtime."""

    def __init__(
        self,
        renderer: TuiRenderer,
        *,
        checker: UpdateStatusChecker = get_update_status,
        installer: UpdateInstaller = install_update,
    ) -> None:
        self._renderer = renderer
        self._checker = checker
        self._installer = installer
        self._installing = False

    @property
    def installing(self) -> bool:
        """Return whether the non-cancellable installation phase has begun."""

        return self._installing

    async def run(self, args: tuple[str, ...]) -> None:
        if args not in {(), ("check",), ("install",)}:
            self._renderer.command_error("Usage: /update [check|install]")
            return

        try:
            status = await self._checker()
        except UpdateCheckError as exc:
            self._renderer.command_error(f"Update check failed: {exc}")
            return

        update = status.available
        if update is None:
            self._renderer.notice(f"Wisp {status.current_version} is up to date.")
            return
        if args != ("install",):
            self._renderer.notice(
                f"Wisp {update.latest_version} is available (current {update.current_version}). "
                "Run /update install to install it."
            )
            return

        try:
            await self._installer(
                update,
                on_install_started=self._mark_installing,
            )
        except UpdateInstallError as exc:
            self._renderer.command_error(f"Update failed: {exc}")
            return
        finally:
            self._installing = False
        self._renderer.notice(
            f"Updated Wisp to {update.latest_version}. Restart Wisp to use the new version."
        )

    def _mark_installing(self) -> None:
        self._installing = True


__all__ = ["UpdateCommands", "UpdateInstaller", "UpdateStatusChecker"]
