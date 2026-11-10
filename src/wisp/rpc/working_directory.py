"""Move the RPC session's working directory, switching projects when it leaves one.

A folder inside the current project root only moves the tools. A folder outside it
belongs to another project, so the move reruns what startup does for that project:
its configuration, trust, permission mode, and skills replace the old project's, and
an undecided project asks for trust at the next prompt, as at startup.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import anyio

from wisp.coding import CodingSession
from wisp.coding.prompt import resolve_project_context_root
from wisp.coding.session import DirectoryChangeError
from wisp.events import (
    ErrorEvent,
    PermissionState,
    ProjectConfigApplied,
    RpcPermissionsReported,
    SessionDirectoryChanged,
    SkillCatalogUpdated,
    WispEvent,
)
from wisp.rpc.configuration import RpcProjectConfiguration
from wisp.rpc.handlers.inspection import rpc_skill_catalog_snapshot
from wisp.runtime.api import WispRuntime
from wisp.sessions.errors import SessionError
from wisp.sessions.jsonl import JsonlSession
from wisp.trust.records import is_trusted, trust_override_from_env

_logger = logging.getLogger(__name__)


class ProjectPermissions(Protocol):
    """Loads a project's saved permission mode."""

    def set_project(self, project_path: Path | None) -> None: ...


class PermissionsReport(Protocol):
    """Reports the permission mode in effect."""

    def permissions_snapshot(self) -> PermissionState: ...


class ProjectTrust(Protocol):
    """Decides a project's trust, asking at the next prompt when undecided."""

    @property
    def project_path(self) -> Path: ...

    @property
    def decision(self) -> bool | None: ...

    def reset(self, project_path: Path, *, decision: bool | None) -> None: ...


@dataclass(frozen=True, slots=True)
class DirectoryMove:
    """What a move changed, for the frontend to hear about."""

    # The saved move, when one was saved.
    change: SessionDirectoryChanged | None
    # The project the session entered; None for a move within the same project.
    project_root: Path | None = None
    # The new project's configuration, when it differs from the old one's.
    applied: ProjectConfigApplied | None = None
    # Warnings about the new project, such as a broad folder that keeps no trust.
    notices: tuple[str, ...] = ()


class RpcWorkingDirectory:
    """Moves the agent between folders, and between projects."""

    def __init__(
        self,
        *,
        agent: CodingSession,
        runtime: WispRuntime,
        project_configuration: RpcProjectConfiguration,
        permissions: ProjectPermissions,
        trust: ProjectTrust,
        project_notices: Callable[[Path], tuple[str, ...]],
    ) -> None:
        self._agent = agent
        self._runtime = runtime
        self._project_configuration = project_configuration
        self._permissions = permissions
        self._trust = trust
        self._project_notices = project_notices
        # Trust decided in this process, by the caller at startup or by a prompt,
        # stays decided for its project when the session moves away and back, as it
        # would if the session had stayed. Keyed by real path.
        self._decided: dict[str, bool] = {}

    async def move(
        self,
        folder: Path,
        *,
        record_in: JsonlSession | None,
        reserve_protected_paths: Callable[[tuple[str, ...]], None] | None = None,
    ) -> DirectoryMove:
        """Work from ``folder``, entering its project if it lies outside this one.

        The move is saved in ``record_in`` before anything changes, so a failed
        save leaves the session where it was. A folder outside the project root
        enters the project that contains it, keeping in-session choices such as an
        explicit model; a folder inside it stays in this project, even when it holds
        a nested repository of its own.

        Args:
            folder (Path): The existing folder to work from, already resolved.
            record_in (JsonlSession | None): The session to save the move in; None
                for a restore, which replays a move already saved.
            reserve_protected_paths (Callable | None): Keeps the new project's
                secrets protected while its configuration is applied.

        Returns:
            DirectoryMove: The saved move and, for a new project, what it changed.

        Raises:
            RuntimeError: While a prompt, compaction, or another folder change runs.
            SessionError: When the move cannot be saved; nothing changed.
            OSError: When the session file cannot be written; nothing changed.
        """

        agent = self._agent
        if agent.contains(folder):
            change = await agent.record_directory_change(record_in, folder)
            agent.move_into(folder)
            return DirectoryMove(change=change)
        root = await anyio.to_thread.run_sync(resolve_project_context_root, folder)
        leaving = self._trust.decision
        if leaving is not None:
            self._decided[os.path.realpath(self._trust.project_path)] = leaving
        # As at startup: a decision this process already made, an environment
        # override, or a saved decision applies without asking, a decline included.
        # With none, the project is untrusted until the next prompt asks.
        decision = self._decided.get(os.path.realpath(root))
        if decision is None:
            decision = await anyio.to_thread.run_sync(_known_decision, root)
        trusted = decision is True
        previous = agent.tool_context.cwd.resolve(strict=False)
        saved: list[SessionDirectoryChanged | None] = []

        async def record() -> None:
            saved.append(await agent.record_directory_change(record_in, folder))

        try:
            applied = await self._project_configuration.apply_project(
                root=root,
                trusted=trusted,
                runtime=self._runtime,
                cwd=folder,
                commit=lambda configuration: agent.move_into(
                    folder, configuration=configuration, project_root=root
                ),
                record=record,
                reserve_protected_paths=reserve_protected_paths,
            )
        except Exception:
            if saved and saved[0] is not None:
                await self._record_way_back(record_in, previous, folder)
            raise
        self._permissions.set_project(root)
        self._trust.reset(root, decision=decision)
        return DirectoryMove(
            change=saved[0] if saved else None,
            project_root=root,
            applied=applied,
            notices=self._project_notices(root),
        )

    async def _record_way_back(
        self, session: JsonlSession | None, previous: Path, entered: Path
    ) -> None:
        """Save a move back after a saved move whose project could not be entered.

        The session file then matches the folder the session still works in, so a
        later resume does not enter the project this move failed to.
        """

        try:
            await self._agent.record_directory_change(session, previous, previous=entered)
        except (SessionError, OSError):
            _logger.exception("Could not save the move back to %s after a failed move", previous)

    async def restore(
        self,
        session: JsonlSession | None,
        *,
        reserve_protected_paths: Callable[[tuple[str, ...]], None] | None = None,
    ) -> DirectoryMove:
        """Work from the folder ``session``'s active path last recorded.

        Called when a session, or a branch of it, becomes the active one. A folder
        in another project enters that project, as ``/cd`` would. A session that
        records no folder works from the launch folder, and so does one whose
        recorded folder is gone or cannot be read, with a notice saying why.

        Args:
            session (JsonlSession | None): The session that became active.
            reserve_protected_paths (Callable | None): Keeps a new project's secrets
                protected while its configuration is applied.

        Returns:
            DirectoryMove: What the restore changed; it saves no move.

        Raises:
            RuntimeError: While a prompt, compaction, or another folder change runs.
        """

        agent = self._agent
        folder = agent.launch_directory
        notices: tuple[str, ...] = ()
        try:
            recorded = await agent.recorded_directory(session)
        except (SessionError, OSError) as exc:
            recorded = None
            notices = (f"Working in {folder}: could not read the session's folder: {exc}",)
        if recorded is not None:
            try:
                folder = agent.resolve_directory(str(recorded))
            except DirectoryChangeError as exc:
                notices = (f"Working in {folder} instead of the session's folder: {exc}",)
        moved = await self.move(
            folder, record_in=None, reserve_protected_paths=reserve_protected_paths
        )
        return DirectoryMove(
            change=None,
            project_root=moved.project_root,
            applied=moved.applied,
            notices=(*notices, *moved.notices),
        )


def directory_move_events(
    agent: CodingSession,
    permissions: PermissionsReport,
    moved: DirectoryMove,
    *,
    command_id: str | None = None,
) -> tuple[WispEvent, ...]:
    """Return the events that tell a frontend what a move into a project changed.

    A move within the same project changes nothing a frontend tracks beyond the
    folder, which ``rpc.workspace`` reports. A move into another project also
    replaces the skills, possibly the provider and model, and the permission mode,
    which is reported for ``command_id`` when there is one.

    Args:
        agent (CodingSession): The agent after the move.
        permissions (PermissionsReport): The approval policy after the move.
        moved (DirectoryMove): What the move changed.
        command_id (str | None): The command that caused the move, if any.

    Returns:
        tuple[WispEvent, ...]: The events, in the order to publish them.
    """

    events: list[WispEvent] = [ErrorEvent(message=notice) for notice in moved.notices]
    if moved.project_root is None:
        return tuple(events)
    if moved.applied is not None:
        events.append(moved.applied)
    events.append(SkillCatalogUpdated(catalog=rpc_skill_catalog_snapshot(agent)))
    if command_id is not None:
        events.append(
            RpcPermissionsReported(
                command_id=command_id, permissions=permissions.permissions_snapshot()
            )
        )
    return tuple(events)


def _known_decision(root: Path) -> bool | None:
    """Return ``root``'s trust decision known without asking, or None if undecided."""

    override = trust_override_from_env()
    if override is not None:
        return override
    return is_trusted(root)


__all__ = ["DirectoryMove", "RpcWorkingDirectory", "directory_move_events"]
