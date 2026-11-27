"""Read the startup preview for the session selected by the RPC host."""

from __future__ import annotations

import os
from pathlib import Path

import anyio

from wisp.cli.startup_pipe import PreviewWorkspace, StartupPreviewPipe
from wisp.events import RpcStartupHistoryPreview
from wisp.sessions.startup_preview import verify_startup_preview

_MAX_BYTES = 512 * 1024
_PREVIEW_TIMEOUT_SECONDS = 0.2


async def read_startup_preview(
    preview: StartupPreviewPipe, selected: Path, session_id: str, cwd: Path
) -> RpcStartupHistoryPreview | None:
    """Read through EOF and check the selected file within one startup deadline.

    Args:
        preview (StartupPreviewPipe): Claimed, nonblocking pipe and expected workspace.
        selected (Path): File chosen by the host for this resume.
        session_id (str): ID of the session chosen by the host.
        cwd (Path): Working directory of the RPC backend.

    Returns:
        RpcStartupHistoryPreview | None: Verified excerpts, or None on timeout or failure.
    """

    try:
        with anyio.move_on_after(_PREVIEW_TIMEOUT_SECONDS) as scope:
            packet = bytearray()
            fd = preview.stream.fileno()
            while len(packet) <= _MAX_BYTES:
                await anyio.wait_readable(fd)
                try:
                    chunk = os.read(fd, min(65536, _MAX_BYTES + 1 - len(packet)))
                except BlockingIOError:
                    continue
                if not chunk:
                    break
                packet.extend(chunk)
            else:
                return None
            if packet:
                # The worker only reads the selected file. It cannot publish a late result.
                lines = await anyio.to_thread.run_sync(
                    verify_startup_preview,
                    bytes(packet),
                    selected.absolute(),
                    session_id,
                    cwd,
                    abandon_on_cancel=True,
                )
                if lines is None:
                    return None
                workspace_matches = await anyio.to_thread.run_sync(
                    lambda: preview.matches_workspace(PreviewWorkspace.current()),
                    abandon_on_cancel=True,
                )
                if workspace_matches and anyio.current_time() < scope.deadline:
                    return RpcStartupHistoryPreview(lines=lines)
    except Exception:
        # Optional pipe, filesystem and validation failures must not affect session replay.
        pass
    return None
