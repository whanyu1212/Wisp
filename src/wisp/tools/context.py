"""Tool execution context."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from wisp.auth.connections import provider_credential_environment
from wisp.config.settings import DEFAULT_PROTECTED_PATHS, user_settings_path
from wisp.mcp.credentials import mcp_auth_path
from wisp.tools.seen_files import SeenFiles
from wisp.trust.permissions import permissions_directory
from wisp.trust.scope import is_broad_root

if TYPE_CHECKING:
    from wisp.config.runtime import WispConfig
    from wisp.tools.files.operations import CreateOnlyWriteReceipt
    from wisp.tools.progress import ToolProgress

# Commands find the context's scratch folder under this name.
SCRATCH_ENVIRONMENT_VARIABLE = "WISP_SCRATCH_DIR"


@dataclass(frozen=True, slots=True)
class WriteConstraint:
    """A narrower write contract for one operation, such as creating one guidance file.

    Attributes:
        allowed_paths: The only exact paths a write may mutate, or ``None`` for any path
            the project-root sandbox allows.
        conflicting_paths: Paths whose existence makes every write fail.
        create_only: Whether writes must pass ``overwrite=false``.
        non_empty: Whether writes must have content.
        receipt: Records the identity of a file a create-only write published.
    """

    allowed_paths: tuple[Path, ...] | None = None
    conflicting_paths: tuple[Path, ...] = ()
    create_only: bool = False
    non_empty: bool = False
    receipt: CreateOnlyWriteReceipt | None = None


@dataclass(frozen=True)
class ToolContext:
    """Ambient state shared with tool invocations."""

    cwd: Path
    # The folder file and search tools may reach. None keeps them inside ``cwd``. A
    # trusted project sets its root, so a session started in a subfolder, or moved
    # into one, can still reach the rest of the project. A broad folder, such as the
    # home folder or a filesystem root, is dropped when the context is built, so a
    # caller-supplied root can never open the whole machine. See ``boundary``.
    workspace_root: Path | None = None
    # A private folder the file and search tools may also reach, beside the
    # workspace, for files that belong to no project (PR bodies, commit messages,
    # logs). None grants nothing; a broad folder is dropped like ``workspace_root``.
    # It must already be resolved, or a write into it would be refused for going
    # through a symbolic link.
    scratch_root: Path | None = None
    # Whether a coding session sets ``scratch_root`` to the running session's own
    # folder at the start of each run. Contexts built from config turn it on.
    session_scratch: bool = False
    max_output_bytes: int = 50_000
    max_output_lines: int = 2_000
    allow_outside_cwd: bool = False
    # ``None`` permits the normal project-root sandbox.
    write_constraint: WriteConstraint | None = None
    # Secure by default: every construction path (including a bare
    # ``ToolContext(cwd=...)`` from embedding/SDK code) protects secrets unless a
    # caller explicitly passes ``protected_paths=()`` to opt out.
    protected_paths: tuple[str, ...] = field(default_factory=lambda: DEFAULT_PROTECTED_PATHS)
    # Environment variables commands do not inherit. Like ``protected_paths``, a bare
    # context withholds Wisp's own provider API keys; pass ``frozenset()`` to opt out.
    withheld_environment: frozenset[str] = field(default_factory=provider_credential_environment)
    # The files the model has seen, which write and edit check before changing one.
    # None leaves the check off, as for tools an embedding calls directly; contexts
    # built from config turn it on, and a coding session keeps one per conversation.
    seen_files: SeenFiles | None = None
    # Where the running call reports progress. The executor sets one per call when
    # a frontend is listening; None means nobody is, and tools skip reporting.
    progress: ToolProgress | None = None

    def __post_init__(self) -> None:
        # Checked once here: ``boundary`` runs for every directory a search walks, and
        # the broad-folder check reads the filesystem.
        if self.workspace_root is not None and is_broad_root(self.workspace_root):
            object.__setattr__(self, "workspace_root", None)
        if self.scratch_root is not None and is_broad_root(self.scratch_root):
            object.__setattr__(self, "scratch_root", None)

    def boundary(self) -> Path:
        """Return the resolved folder the file and search tools must stay inside.

        The workspace root applies only while it contains ``cwd`` and is not a broad
        folder; otherwise the tools stay inside ``cwd``, so a root that does not fit
        never widens access.
        ``allow_outside_cwd`` lifts the boundary separately.

        Returns:
            Path: The resolved workspace root, or the resolved ``cwd``.
        """

        cwd = self.cwd.resolve(strict=False)
        if self.workspace_root is None:
            return cwd
        root = self.workspace_root.resolve(strict=False)
        return root if cwd.is_relative_to(root) else cwd

    def boundary_for(self, path: Path) -> Path | None:
        """Return the allowed folder that contains ``path``, if any.

        A walk or open that starts inside the scratch folder stays inside it, and
        one that starts in the workspace stays inside ``boundary()``.
        ``allow_outside_cwd`` lifts the boundary separately.

        Args:
            path (Path): An absolute path, lexical or resolved.

        Returns:
            Path | None: ``boundary()`` or ``scratch_root`` when either contains
            ``path``; otherwise None.
        """

        boundary = self.boundary()
        if path.is_relative_to(boundary):
            return boundary
        if self.scratch_root is not None and path.is_relative_to(self.scratch_root):
            return self.scratch_root
        return None

    @classmethod
    def default(cls) -> ToolContext:
        """Create a context rooted at the current working directory."""

        return cls(cwd=Path.cwd())

    @classmethod
    def from_config(
        cls,
        config: WispConfig,
        *,
        cwd: Path | None = None,
        workspace_root: Path | None = None,
    ) -> ToolContext:
        """Create a context that honors a config's protected-path policy.

        ``cwd`` defaults to the current working directory, and ``workspace_root``
        to none, which keeps the file tools inside ``cwd``. The protected-path
        globs come from the resolved config so file tools deny reads of secrets
        (``.env`` and friends) unless the policy is relaxed. A coding session gives
        each run of such a context its session's scratch folder.

        The active credential, MCP credential, user settings, and provider CA bundle files
        are guaranteed to be in the returned context's ``protected_paths``. ``WispConfig`` already
        enforces this on construction; re-asserting it here is a defensive backstop so
        the secrets stay protected even for a config produced by a path that skipped
        validation (e.g. ``model_copy``).
        """

        protected_paths = config.protected_paths
        required = (
            config.auth_path.expanduser().resolve(strict=False).as_posix(),
            mcp_auth_path().resolve(strict=False).as_posix(),
            user_settings_path().resolve(strict=False).as_posix(),
            permissions_directory().resolve(strict=False).as_posix(),
            f"{permissions_directory().resolve(strict=False).as_posix()}/**",
            *(
                (config.openai_compatible.ca_bundle.expanduser().resolve(strict=False).as_posix(),)
                if config.openai_compatible is not None
                and config.openai_compatible.ca_bundle is not None
                else ()
            ),
        )
        missing = tuple(pattern for pattern in required if pattern not in protected_paths)
        if missing:
            protected_paths = (*protected_paths, *missing)

        endpoint = config.openai_compatible
        return cls(
            cwd=cwd if cwd is not None else Path.cwd(),
            workspace_root=workspace_root,
            session_scratch=True,
            protected_paths=protected_paths,
            seen_files=SeenFiles(),
            withheld_environment=(
                provider_credential_environment()
                if endpoint is None
                else provider_credential_environment(
                    openai_compatible_provider=endpoint.provider_name
                )
            ),
        )
