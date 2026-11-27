"""Private descriptor handoff between the native launcher and its RPC backend."""

from __future__ import annotations

import os
import re
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

PREVIEW_FD_PREFIX = "--startup-preview-fd="


@dataclass(frozen=True)
class PreviewWorkspace:
    """Directory names and filesystem identities observed at the trust decision."""

    project_root: Path
    root_identity: tuple[int, int]
    cwd: Path
    cwd_identity: tuple[int, int]

    @classmethod
    def capture(cls, project_root: Path, cwd: Path) -> PreviewWorkspace | None:
        """Snapshot the directory identities used by a CLI trust decision.

        Args:
            project_root (Path): Resolved directory passed to the trust check.
            cwd (Path): Absolute process working directory used to discover the root.

        Returns:
            PreviewWorkspace | None: Snapshot if both directories can be identified
                and the cwd path still names the process working directory.
        """
        try:
            root = project_root.stat()
            working = cwd.stat()
            if not stat.S_ISDIR(root.st_mode) or not os.path.samestat(working, os.stat(".")):
                return None
            return cls(
                project_root, (root.st_dev, root.st_ino), cwd, (working.st_dev, working.st_ino)
            )
        except OSError:
            return None

    @classmethod
    def current(cls) -> PreviewWorkspace | None:
        """Rediscover the root so a newly selected project also invalidates a preview."""
        from wisp.coding.prompt import resolve_project_context_root

        try:
            cwd = Path.cwd()
            return cls.capture(resolve_project_context_root(cwd), cwd)
        except (OSError, RuntimeError, ValueError):
            return None

    def token(self) -> str:
        """Fingerprint both paths and identities for comparison across processes."""
        import hashlib
        import json

        identity = json.dumps(
            (str(self.project_root), self.root_identity, str(self.cwd), self.cwd_identity)
        )
        return hashlib.sha256(identity.encode("ascii")).hexdigest()

    def fd_argument(self, fd: int) -> str:
        """Bind the inherited reader to this workspace in the private backend argument."""
        return f"{PREVIEW_FD_PREFIX}{fd}:{self.token()}"


@dataclass(frozen=True)
class StartupPreviewPipe:
    """Own the preview reader from entry to consumption or startup failure."""

    stream: BinaryIO
    workspace_token: str

    @classmethod
    def claim(cls, argument: str) -> StartupPreviewPipe | None:
        """Claim only an inherited read pipe distinct from all RPC standard streams."""

        if os.name != "posix":
            return None
        import fcntl

        try:
            if not argument.startswith(PREVIEW_FD_PREFIX):
                return None
            descriptor, _, token = argument[len(PREVIEW_FD_PREFIX) :].partition(":")
            if re.fullmatch(r"[0-9a-f]{64}", token) is None:
                return None
            fd = int(descriptor)
            if fd < 3 or not os.get_inheritable(fd):
                return None
            info = os.fstat(fd)
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            if flags & (os.O_ACCMODE | getattr(os, "O_PATH", 0)):
                return None
            if not stat.S_ISFIFO(info.st_mode) or _aliases_stdio(info):
                return None
        except (OSError, OverflowError, ValueError):
            return None

        # Only a validated reader belongs to us, including cleanup on setup failure.
        try:
            os.set_inheritable(fd, False)
            os.set_blocking(fd, False)
            return cls(os.fdopen(fd, "rb", buffering=0), token)
        except OSError:
            os.close(fd)
            return None

    def matches_workspace(self, workspace: PreviewWorkspace | None) -> bool:
        """Check the parent's fingerprint against an independently observed workspace."""
        return workspace is not None and workspace.token() == self.workspace_token


def _aliases_stdio(info: os.stat_result) -> bool:
    for fd in (0, 1, 2):
        with suppress(OSError):
            if os.path.samestat(info, os.fstat(fd)):
                return True
    return False
