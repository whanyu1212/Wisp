"""macOS containment for exact automatically approved shell calls.

Seatbelt is a pathname boundary, not inode isolation: pre-existing hardlinks can
alias protected files. Never use this policy as a guarantee of credential secrecy.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from wisp.tools.files.paths import matches_protected_pattern
from wisp.tools.result import ToolError
from wisp.trust.scope import is_broad_root

if TYPE_CHECKING:
    from wisp.tools.approval import ToolApprovalPolicy
    from wisp.tools.context import ToolContext

_EXECUTABLE = "/usr/bin/sandbox-exec"
_ACTIVE: ContextVar[SeatbeltLaunch | None] = ContextVar("seatbelt_launch", default=None)
_BASE = """(version 1)
(deny default)
(allow process-exec process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))
(allow sysctl-read
  (sysctl-name-prefix "hw.")
  (sysctl-name-prefix "kern.os")
  (sysctl-name-prefix "kern.proc.pid.")
  (sysctl-name-prefix "kern.proc.pgrp.")
  (sysctl-name "kern.argmax")
  (sysctl-name "kern.hostname")
  (sysctl-name "kern.maxfilesperproc")
  (sysctl-name "kern.maxproc")
  (sysctl-name "kern.usrstack64")
  (sysctl-name "kern.version")
  (sysctl-name "kern.sysv.semmns")
  (sysctl-name "sysctl.proc_cputype")
  (sysctl-name "vm.loadavg"))
(allow file-read-metadata)
(allow file-read-data (literal "/"))
(allow file-write-data (require-all (literal "/dev/null") (vnode-type CHARACTER-DEVICE)))
(allow ipc-posix-sem)
(deny mach-lookup (xpc-service-name-prefix ""))
(deny file-write-flags)
(deny system-fcntl (fcntl-command 80 110))
"""
# F_MAKECOMPRESSED/F_TRANSFEREXTENTS can mutate read-only descriptors. This
# explicit denial follows the finding documented by OpenAI Codex's Seatbelt tests.
_CONTROL_NAMES = (".git", ".codex", ".claude", ".agents", "AGENTS.md", "CLAUDE.md", "CODEX.md")
_SKIP_SYMLINK_SCAN_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "target",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
    }
)
_MAX_SYMLINK_SCAN_ENTRIES = 100_000
_PRIVATE_NAMES = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".azure",
    ".kube",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".pgpass",
    "credentials.json",
)


@dataclass(frozen=True, slots=True)
class ProcessAuthority:
    """Session-, workspace-, and consent-bound authority for sandboxed job handles."""

    owner: object
    policy: ToolApprovalPolicy
    generation: int
    workspace: Path

    def active(self, context: ToolContext) -> bool:
        """Check live consent and scope without granting a different tool or operation."""
        return (
            self.owner is context.process_owner
            and self.policy.mode == "auto_commands"
            and self.policy.generation == self.generation
            and self.workspace == context.boundary()
            and not context.allow_outside_cwd
            and context.write_constraint is None
            and not context.atomic_writes_only
        )

    def matches(self, other: ProcessAuthority) -> bool:
        """Compare identities, not structural equality of mutable approval policies."""
        return (
            self.owner is other.owner
            and self.policy is other.policy
            and self.generation == other.generation
            and self.workspace == other.workspace
        )


@dataclass(slots=True)
class _EphemeralDirLease:
    """Single-owner lifecycle handle for a per-command sandbox temp directory."""

    path: Path
    claimed: bool = False

    def claim(self) -> Path | None:
        """Transfer ownership to a spawned subprocess; subsequent cleanups no-op."""
        if self.claimed:
            return None
        self.claimed = True
        return self.path

    def release(self) -> None:
        """Delete the directory if ownership has not been transferred."""
        if self.claimed:
            return
        self.claimed = True
        cleanup_ephemeral_dir(self.path)


@dataclass(frozen=True, slots=True)
class SeatbeltLaunch:
    """Immutable authority bound to the exact command and working directory."""

    command: str
    cwd: Path
    profile: str
    environment: tuple[tuple[str, str], ...]
    roots: tuple[Path, ...]
    authority: ProcessAuthority | None = None
    cleanup_dir: Path | None = None
    read_only_workspace: bool = False
    _lease: _EphemeralDirLease | None = None

    def claim_cleanup_dir(self) -> Path | None:
        """Transfer ephemeral directory cleanup responsibility to a spawned process."""
        if self._lease is not None:
            return self._lease.claim()
        return self.cleanup_dir

    def cleanup(self) -> None:
        """Remove this launch's ephemeral directory if ownership was not transferred."""
        if self._lease is not None:
            self._lease.release()
            return
        cleanup_ephemeral_dir(self.cleanup_dir)

    def validate(self, command: str, cwd: Path) -> None:
        """Reject rebinding or symlinked roots before spawning any command."""
        if self.authority is not None and (
            self.authority.policy.mode != "auto_commands"
            or self.authority.policy.generation != self.authority.generation
        ):
            raise ToolError("Automatic command consent was revoked before launch.")
        if command != self.command or cwd.resolve() != self.cwd:
            raise ToolError("Sandbox launch no longer matches the approved command.")
        for root in self.roots:
            _validate_root(root)

    def argv(self, wrapped_command: str, jobs_file: Path) -> list[str]:
        """Add only the host-owned job recorder to the prepared policy."""
        recorder = f"(allow file-write-data (literal {_sbpl_string(str(jobs_file.resolve()))}))"
        return [_EXECUTABLE, "-p", self.profile + recorder, "/bin/sh", "-c", wrapped_command]


def _make_tree_removable(root: Path) -> None:
    """Restore owner permissions and clear UF_IMMUTABLE without following symlinks."""
    chflags = getattr(os, "chflags", None)
    uf_immutable = getattr(stat, "UF_IMMUTABLE", 0)
    stack = [root]
    while stack:
        current = stack.pop()
        if current.is_symlink():
            continue
        if chflags is not None and uf_immutable:
            try:
                info = current.lstat()
                if getattr(info, "st_flags", 0) & uf_immutable:
                    chflags(current, info.st_flags & ~uf_immutable)
            except OSError:
                pass
        try:
            os.chmod(current, 0o700)
        except OSError:
            pass
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        continue
                    entry_path = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry_path)
                    elif chflags is not None and uf_immutable:
                        try:
                            info = entry.stat(follow_symlinks=False)
                            if getattr(info, "st_flags", 0) & uf_immutable:
                                chflags(entry_path, info.st_flags & ~uf_immutable)
                        except OSError:
                            pass
        except OSError:
            pass


def cleanup_ephemeral_dir(path: Path | None) -> None:
    """Remove an ephemeral sandbox directory even if the child tightened permissions."""
    if path is None or path.is_symlink() or not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError:
        _make_tree_removable(path)
        shutil.rmtree(path, ignore_errors=True)


@contextmanager
def activate_seatbelt(launch: SeatbeltLaunch | None) -> Iterator[None]:
    """Bind one tool invocation's launch, inherited by its supervisor tasks."""
    token = _ACTIVE.set(launch)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def active_seatbelt() -> SeatbeltLaunch | None:
    """Return the current call's launch, never a process-wide permission flag."""
    return _ACTIVE.get()


def _sandbox_python_test(tokens: list[str]) -> bool:
    if not tokens:
        return False
    if tokens[0] in {"python", "python3"}:
        return tokens[1:3] in (["-m", "pytest"], ["-m", "unittest"])
    return tokens[0] == "pytest"


def sandbox_development_command(arguments: Mapping[str, object]) -> bool:
    """Recognize the fast path after the shared command eligibility checks pass."""
    command = arguments.get("command")
    if not isinstance(command, str):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False
    if tokens[:2] == ["uv", "run"]:
        if tokens[2:3] != ["--no-sync"]:
            return False
        return _sandbox_python_test(tokens[3:])
    if _sandbox_python_test(tokens):
        return True
    if tokens[0] == "cargo":
        return tokens[1:2] in (["test"], ["check"], ["build"])
    if tokens[0] == "npm":
        return tokens[1:2] == ["test"] or tokens[1:3] in (
            ["run", "test"],
            ["run", "build"],
        )
    return False


def _has_ancestor_cargo_lock(cwd: Path, workspace: Path) -> bool:
    current = cwd
    while True:
        if (current / "Cargo.lock").is_file():
            return True
        if current == workspace or current.parent == current:
            return False
        current = current.parent


def _sandbox_test_profile(command: str, cwd: Path, workspace: Path) -> tuple[bool, bool]:
    """Return ``(read_only_workspace, needs_cargo_target)`` for a sandboxed command.

    Tests write fixtures into their fresh per-command TMPDIR, keeping the
    workspace read-only. Only ``cargo test`` with an existing workspace Cargo.lock
    receives the isolated session Cargo target directory; crates without a
    Cargo.lock keep writable-workspace mode so Cargo can generate Cargo.lock.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False, False
    if tokens[:2] == ["uv", "run"]:
        if tokens[2:3] != ["--no-sync"]:
            return False, False
        return _sandbox_python_test(tokens[3:]), False
    if _sandbox_python_test(tokens):
        return True, False
    if tokens[:2] == ["cargo", "test"]:
        if _has_ancestor_cargo_lock(cwd, workspace):
            return True, True
        return False, False
    if tokens[:2] == ["npm", "test"] or tokens[:3] == ["npm", "run", "test"]:
        return True, False
    return False, False


def _has_control_chars(text: str) -> bool:
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in text)


def _sbpl_string(text: str) -> str:
    """Serialize a literal SBPL string while preserving UTF-8 path bytes."""
    if _has_control_chars(text):
        raise ToolError("Sandbox path contains unsupported control characters.")
    return json.dumps(text, ensure_ascii=False)


def _sbpl_regex(pattern: str) -> str:
    """Serialize an SBPL raw regex literal, failing closed on quotes or control chars."""
    if '"' in pattern or _has_control_chars(pattern):
        raise ToolError("Sandbox cannot safely compile this path or protected pattern.")
    return f'#"{pattern}"'


def _validate_root(root: Path) -> None:
    text = str(root)
    if '"' in text or _has_control_chars(text):
        raise ToolError("Sandbox root path contains unsupported characters.")
    if not root.is_absolute() or is_broad_root(root) or not root.is_dir():
        raise ToolError("Sandbox requires an existing, non-broad workspace and scratch directory.")
    # Normalize system aliases (/tmp, /var), but do not follow user-owned ones.
    for ancestor in (root, *root.parents):
        if ancestor.parent != Path("/") and ancestor.is_symlink():
            raise ToolError("Sandbox writable roots cannot contain symbolic links.")


def _case_literal(text: str) -> str:
    return "".join(
        f"[{char.lower()}{char.upper()}]" if char.isascii() and char.isalpha() else re.escape(char)
        for char in text
    )


def _protected_pattern(pattern: str) -> str:
    # The file tools treat a leading **/ as a suffix match. A trailing /**
    # protects a whole subtree, including the mandatory permissions directory.
    # Configured credential paths are already expanded; custom globs stay literal.
    expanded = pattern
    if expanded.startswith("**/"):
        expanded = expanded[3:]
    if expanded.endswith("/**"):
        expanded = expanded[:-3]
    # Unsupported glob syntax is an admission failure, not an ignored protection.
    if (
        not expanded.strip("/")
        or any(char in expanded for char in '"[]{}\\')
        or _has_control_chars(expanded)
        or ("/" in expanded and any(char in expanded for char in "*?"))
    ):
        raise ToolError("Sandbox cannot enforce this protected-path pattern.")
    return expanded


def _protected_regex(pattern: str, *, subtree: bool = True) -> str:
    # Match trailing segments at every depth, as the file tools do, rather than
    # silently weakening slash-bearing relative protections to one workspace path.
    parts = _protected_pattern(pattern).lstrip("/").split("*")
    expression = "[^/]*".join(
        "[^/]".join(_case_literal(part) for part in piece.split("?")) for piece in parts
    )
    return "^(.*/)?" + expression + ("(/.*)?$" if subtree else "$")


def prepare_seatbelt(arguments: Mapping[str, object], context: ToolContext) -> SeatbeltLaunch:
    """Prepare and probe containment, failing closed before automatic approval.

    Args:
        arguments (Mapping[str, object]): Schema-checked exact Bash arguments.
        context (ToolContext): Workspace, scratch and protected paths.

    Returns:
        SeatbeltLaunch: The exact launch authority; never an unrestricted fallback.

    Raises:
        ToolError: The platform, paths, policy or runtime cannot enforce containment.
    """
    if sys.platform != "darwin" or not Path(_EXECUTABLE).is_file():
        raise ToolError(
            "Automatic commands require macOS Seatbelt; approve unrestricted access manually."
        )
    if context.allow_outside_cwd or context.write_constraint is not None:
        raise ToolError("This tool context cannot use automatic sandboxed commands.")
    command = arguments.get("command")
    if not isinstance(command, str):
        raise ToolError("Sandbox requires an exact command.")
    # Validate lexical roots before resolving so user-controlled symlinks are not erased.
    _validate_root(context.cwd)
    if context.workspace_root is not None:
        _validate_root(context.workspace_root)
    if context.scratch_root is None:
        raise ToolError("Automatic commands require a private session scratch directory.")
    _validate_root(context.scratch_root)
    cwd = _resolve_safely(context.cwd, "Sandbox working directory could not be resolved safely.")
    if context.workspace_root is not None:
        resolved_workspace = _resolve_safely(
            context.workspace_root, "Sandbox workspace root could not be resolved safely."
        )
        if not cwd.is_relative_to(resolved_workspace):
            raise ToolError("Sandbox working directory must stay inside the workspace root.")
    workspace = context.boundary()
    scratch = _resolve_safely(
        context.scratch_root, "Sandbox scratch directory could not be resolved safely."
    )
    roots = (workspace, scratch)
    for root in roots:
        _validate_root(root)
    read_only_workspace, needs_cargo_target = _sandbox_test_profile(command, cwd, workspace)
    runs_root = scratch / ".seatbelt-runs"
    cargo_target_root = scratch / ".seatbelt-cargo-target"
    try:
        if runs_root.is_symlink():
            raise ToolError("Sandbox run directory cannot be a symbolic link.")
        runs_root.mkdir(mode=0o700, exist_ok=True)
        _validate_root(runs_root)
        run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=runs_root)).resolve()
    except ToolError:
        raise
    except OSError as exc:
        raise ToolError("Could not create private sandbox temp directory.") from exc
    try:
        tmp_dir = run_dir / "tmp"
        home_dir = run_dir / "home"
        tmp_dir.mkdir(mode=0o700)
        home_dir.mkdir(mode=0o700)
        cargo_target: Path | None = None
        if needs_cargo_target:
            if cargo_target_root.is_symlink():
                raise ToolError("Sandbox Cargo target directory cannot be a symbolic link.")
            cargo_target_root.mkdir(mode=0o700, exist_ok=True)
            _validate_root(cargo_target_root)
            cargo_target = cargo_target_root.resolve()
        _validate_root(run_dir)
        reads, environment = _runtime_access(
            roots,
            run_dir=run_dir,
            tmp_dir=tmp_dir,
            home_dir=home_dir,
            cargo_target=cargo_target,
            read_only_workspace=read_only_workspace,
        )
        for key in context.withheld_environment:
            environment.pop(key, None)
        profile = _filesystem_profile(
            reads,
            roots,
            context.protected_paths,
            runs_root=runs_root.resolve(),
            run_dir=run_dir,
            tmp_dir=tmp_dir,
            home_dir=home_dir,
            cargo_target_root=cargo_target_root,
            cargo_target=cargo_target,
            read_only_workspace=read_only_workspace,
        )
        try:
            probe = subprocess.run(
                [_EXECUTABLE, "-p", profile, "/usr/bin/true"],
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolError(
                "Seatbelt capability probe failed; automatic execution stopped."
            ) from exc
        if probe.returncode != 0:
            raise ToolError("Seatbelt policy could not be applied; automatic execution stopped.")
    except BaseException:
        cleanup_ephemeral_dir(run_dir)
        raise
    validated_roots = (
        (workspace, scratch, runs_root.resolve(), run_dir, cargo_target)
        if cargo_target is not None
        else (workspace, scratch, runs_root.resolve(), run_dir)
    )
    return SeatbeltLaunch(
        command,
        cwd,
        profile,
        tuple(environment.items()),
        validated_roots,
        cleanup_dir=run_dir,
        read_only_workspace=read_only_workspace,
        _lease=_EphemeralDirLease(run_dir),
    )


def _runtime_access(
    roots: tuple[Path, Path],
    *,
    run_dir: Path,
    tmp_dir: Path,
    home_dir: Path,
    cargo_target: Path | None,
    read_only_workspace: bool,
) -> tuple[set[Path], dict[str, str]]:
    """Resolve installed toolchains and construct a secret-free environment."""
    workspace, scratch = roots
    home = Path.home()
    reads = {
        Path(path)
        for path in (
            "/System",
            "/usr/bin",
            "/usr/lib",
            "/usr/share",
            "/bin",
            "/sbin",
            "/Library/Developer",
            "/Library/Apple",
            # xcrun checks the system's existing Xcode license receipt. This is
            # read-only, not permission to accept a license or change preferences.
            "/Library/Preferences/com.apple.dt.Xcode.plist",
            "/private/var/db/timezone",
            "/private/etc/localtime",
            "/private/etc/hosts",
            "/private/etc/passwd",
            "/private/etc/group",
            "/private/etc/ssl/openssl.cnf",
            "/opt/homebrew/etc/openssl@3/openssl.cnf",
            "/opt/homebrew/etc/openssl@4/openssl.cnf",
            "/usr/local/etc/openssl@3/openssl.cnf",
            "/usr/local/etc/openssl@4/openssl.cnf",
            "/dev/null",
            "/dev/zero",
            "/dev/random",
            "/dev/urandom",
            "/dev/fd",
        )
    }
    reads.add(workspace)
    reads.add(run_dir)
    if cargo_target is not None:
        reads.add(cargo_target)
    if not read_only_workspace:
        reads.add(scratch)
    reads.update((Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()))
    # uv overlays can reuse another environment's installed packages via .pth.
    # Admit package directories, never arbitrary project entries from sys.path.
    reads.update(
        Path(path).resolve()
        for path in sys.path
        if Path(path).name in {"site-packages", "dist-packages"}
    )
    reads.update((home / ".cargo/bin", home / ".rustup", home / ".cargo/registry"))
    # Standard package-manager installations, read-only. No user home-wide grant.
    for installation in ("/opt/homebrew/Cellar", "/usr/local/Cellar"):
        if Path(installation).is_dir():
            reads.add(Path(installation))
    selection = Path("/var/db/xcode_select_link")
    if selection.exists():
        developer = selection.resolve()
        if developer.name == "Developer" and developer.parent.name == "Contents":
            # Full Xcode installs keep linker dependencies in the selected bundle's
            # sibling Frameworks directories, outside Contents/Developer.
            reads.add(developer.parent.parent)
        else:
            reads.add(developer)
    paths = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    for name in (
        "uv",
        "python",
        "python3",
        "pytest",
        "cargo",
        "rustc",
        "node",
        "npm",
        "ruff",
        "mypy",
        "go",
    ):
        executable = shutil.which(name)
        if executable is None:
            continue
        if not Path(executable).is_absolute():
            raise ToolError("Relative toolchain search paths need explicit approval.")
        resolved = Path(executable).resolve()
        directory = resolved.parent
        # npm needs its sibling modules; node version-manager installations are scoped
        # to one version, not the entire home directory or manager configuration.
        in_virtualenv = (directory.parent / "pyvenv.cfg").is_file()
        read_root = directory.parent if name in {"node", "npm"} or in_virtualenv else directory
        if is_broad_root(read_root):
            raise ToolError("A toolchain resolves to an overly broad sandbox read root.")
        reads.add(read_root)
        paths.insert(0, str(Path(executable).parent))
    # Preserve the user's toolchain precedence rather than promoting a later
    # tool's directory (for example Homebrew Go) over their selected Node version.
    paths = [directory for directory in os.get_exec_path() if directory in paths] + paths
    paths.insert(0, str(Path(sys.prefix) / "bin"))
    paths.insert(0, str(workspace / ".venv/bin"))
    # Cache/home writes stay in the per-command ephemeral directory under scratch.
    # Programs may create caches there; preparation itself does not mutate the workspace.
    environment = {
        "PATH": os.pathsep.join(dict.fromkeys(paths)),
        "HOME": str(home_dir),
        "TMPDIR": str(tmp_dir),
        "WISP_SCRATCH_DIR": str(tmp_dir if read_only_workspace else scratch),
        "LANG": "en_US.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "UV_CACHE_DIR": str(run_dir / "uv-cache"),
        "npm_config_cache": str(run_dir / "npm-cache"),
        "CARGO_HOME": str(home / ".cargo"),
        "CARGO_NET_OFFLINE": "true",
        "RUSTUP_HOME": str(home / ".rustup"),
    }
    if cargo_target is not None:
        environment["CARGO_TARGET_DIR"] = str(cargo_target)
    return reads, environment


def _resolve_safely(path: Path, message: str) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError) as exc:
        raise ToolError(message) from exc


def _package_roots(
    reads: set[Path], roots: tuple[Path, Path], *, excluded_prefixes: set[Path]
) -> tuple[Path, ...]:
    """Locate installed Python packages, not arbitrary project source directories."""
    workspace, scratch = roots
    venv = workspace / ".venv"
    venv_is_symlink = venv.is_symlink()
    venv_resolved = (
        _resolve_safely(venv, "Workspace virtual environment could not be resolved safely.")
        if venv.exists() or venv_is_symlink
        else None
    )
    candidates = {path for path in reads if path.name in {"site-packages", "dist-packages"}}
    # Never treat writable workspace, scratch, or ephemeral roots as virtualenv prefixes.
    for prefix in (*(reads - excluded_prefixes), venv):
        if (prefix / "pyvenv.cfg").is_file():
            candidates.update((prefix / "lib").glob("python*/site-packages"))
    packages: set[Path] = set()
    for candidate in candidates:
        resolved = _resolve_safely(
            candidate, "Python package directory could not be resolved safely."
        )
        if resolved.name not in {"site-packages", "dist-packages"} or is_broad_root(resolved):
            raise ToolError("Python package directory resolves outside an installed package root.")
        if resolved.is_relative_to(scratch):
            raise ToolError("Python package directory cannot resolve inside session scratch.")
        if resolved.is_relative_to(workspace) and (
            venv_is_symlink or venv_resolved is None or not resolved.is_relative_to(venv_resolved)
        ):
            raise ToolError("Python package directory cannot resolve into the writable workspace.")
        if resolved.is_dir():
            packages.add(resolved)
    return tuple(sorted(packages))


def _package_protections(
    packages: tuple[Path, ...],
    workspace: Path,
    *,
    ephemeral_exception: str | None = None,
) -> list[str]:
    """Allow public credential-module source only in pinned, read-only packages.

    Explicit configured protections are applied separately and still win. All
    workspace .venv writes, package writes, and ancestor moves are denied so a
    workspace secret cannot be relocated into this read exception across calls.
    In read-only-workspace test runs, the fresh ephemeral temp/home directories
    are exempt so tests can build synthetic fixtures without exposing workspace
    or host credentials.
    Pre-existing hardlink aliases remain the documented shared-workspace limitation.
    """
    rules: list[str] = []
    # Keep workspace/.venv non-writable even when absent, blocking cross-call planting
    # of a fake pyvenv.cfg or sibling python*/site-packages directory.
    venv_regex = "^" + _case_literal(str(workspace / ".venv")) + "(/.*)?$"
    rules.append(f"(deny file-write* (regex {_sbpl_regex(venv_regex)}))")
    for package in packages:
        rules.append(f"(deny file-write* (subpath {_sbpl_string(str(package))}))")
        for ancestor in (package, *package.parents):
            rules.append(
                "(deny file-write-unlink (require-all (vnode-type DIRECTORY) "
                f"(literal {_sbpl_string(str(ancestor))})))"
            )
    credentials = _sbpl_regex(_protected_regex("credentials"))
    if ephemeral_exception is not None:
        rules.append(
            f"(deny file-write* (require-all (regex {credentials}) "
            f"(require-not {ephemeral_exception})))"
        )
    else:
        rules.append(f"(deny file-write* (regex {credentials}))")
    read_exceptions: list[str] = []
    if packages:
        locations = " ".join(f"(subpath {_sbpl_string(str(path))})" for path in packages)
        read_exceptions.append(
            f"(require-all (require-any {locations}) "
            "(require-any (vnode-type DIRECTORY) "
            '(require-all (vnode-type REGULAR-FILE) (regex #"[.]py$"))))'
        )
    if ephemeral_exception is not None:
        read_exceptions.append(ephemeral_exception)
    if read_exceptions:
        allowed = (
            read_exceptions[0]
            if len(read_exceptions) == 1
            else f"(require-any {' '.join(read_exceptions)})"
        )
        rules.append(
            f"(deny file-read* (require-all (regex {credentials}) (require-not {allowed})))"
        )
    else:
        rules.append(f"(deny file-read* (regex {credentials}))")
    return rules


def _bind_symlink_target(
    rules: list[str],
    path: Path,
    *,
    seen: set[Path],
    operations: str,
) -> None:
    """Deny a symlinked protected/control path's target and pin both names' ancestors."""
    resolved = _resolve_safely(path, "Protected symlink target could not be resolved safely.")
    if resolved != path and resolved not in seen:
        seen.add(resolved)
        rules.append(f"(deny {operations} (subpath {_sbpl_string(str(resolved))}))")
    for protected in (path, resolved):
        for ancestor in protected.parents:
            rules.append(
                "(deny file-write-unlink (require-all (vnode-type DIRECTORY) "
                f"(literal {_sbpl_string(str(ancestor))})))"
            )


def _bind_workspace_symlinks(
    rules: list[str],
    workspace: Path,
    *,
    protected_patterns: tuple[str, ...],
    seen_protected: set[Path],
    seen_control: set[Path],
) -> None:
    """Bind nested protected and control symlinks without following directory links."""
    scanned = 0
    stack = [str(workspace)]
    try:
        while stack:
            current = stack.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > _MAX_SYMLINK_SCAN_ENTRIES:
                        raise ToolError(
                            "Workspace exceeds sandbox symlink inspection limit; approve manually."
                        )
                    if entry.is_symlink():
                        symlink = Path(entry.path)
                        if matches_protected_pattern(symlink, protected_patterns):
                            _bind_symlink_target(
                                rules,
                                symlink,
                                seen=seen_protected,
                                operations="file-read* file-write*",
                            )
                        elif matches_protected_pattern(symlink, _CONTROL_NAMES):
                            _bind_symlink_target(
                                rules,
                                symlink,
                                seen=seen_control,
                                operations="file-write*",
                            )
                    elif (
                        entry.is_dir(follow_symlinks=False)
                        and entry.name not in _SKIP_SYMLINK_SCAN_DIRS
                    ):
                        stack.append(entry.path)
    except OSError as exc:
        raise ToolError("Sandbox could not inspect workspace entries.") from exc


def _filesystem_profile(
    reads: set[Path],
    roots: tuple[Path, Path],
    protected_paths: tuple[str, ...],
    *,
    runs_root: Path,
    run_dir: Path,
    tmp_dir: Path,
    home_dir: Path,
    cargo_target_root: Path,
    cargo_target: Path | None,
    read_only_workspace: bool,
) -> str:
    """Compile read/write grants with final path and ancestor protections."""
    workspace, scratch = roots
    excluded_prefixes = {workspace, scratch, runs_root, run_dir}
    if cargo_target is not None:
        excluded_prefixes.add(cargo_target)
    packages = _package_roots(reads, roots, excluded_prefixes=excluded_prefixes)
    rules = [_BASE]
    for root in sorted(reads):
        resolved_root = root.resolve()
        if is_broad_root(resolved_root):
            raise ToolError("A sandbox read root resolves to a broad directory.")
        rules.append(f"(allow file-read* (subpath {_sbpl_string(str(resolved_root))}))")
    writable_roots: tuple[Path, ...] = (
        (run_dir, cargo_target)
        if read_only_workspace and cargo_target is not None
        else ((run_dir,) if read_only_workspace else (workspace, scratch, run_dir))
    )
    for root in writable_roots:
        quoted = _sbpl_string(str(root))
        rules.append(f"(allow file-write* (subpath {quoted}))")
    pinned_roots = (*roots, runs_root, run_dir, tmp_dir, home_dir) + (
        (cargo_target,) if cargo_target is not None else ()
    )
    for root in dict.fromkeys(pinned_roots):
        for ancestor in (root, *root.parents):
            rules.append(
                "(deny file-write-unlink (require-all (vnode-type DIRECTORY) "
                f"(literal {_sbpl_string(str(ancestor))})))"
            )
    # Isolate per-command run directories and the read-only Cargo test target
    # cache from writable-workspace commands so nothing created under a
    # read-only test's ephemeral TMPDIR can ever cross into the workspace.
    if not read_only_workspace:
        rules.append(
            "(deny file-read* file-write* "
            f"(require-all (subpath {_sbpl_string(str(runs_root))}) "
            f"(require-not (subpath {_sbpl_string(str(run_dir))}))))"
        )
    if cargo_target is None:
        cargo_regex = _sbpl_regex("^" + _case_literal(str(cargo_target_root)) + "(/.*)?$")
        rules.append(f"(deny file-read* file-write* (regex {cargo_regex}))")
        rules.append(
            f"(deny file-write-unlink (require-all (vnode-type DIRECTORY) (regex {cargo_regex})))"
        )
    # Scope Unix domain sockets strictly to this command's fresh private TMPDIR so
    # tests can use local IPC without touching host daemons or TCP loopback.
    quoted_tmp = _sbpl_string(str(tmp_dir))
    rules.append(f"(allow network-bind (local unix-socket (subpath {quoted_tmp})))")
    rules.append(f"(allow network-inbound (local unix-socket (subpath {quoted_tmp})))")
    rules.append(f"(allow network-outbound (remote unix-socket (subpath {quoted_tmp})))")

    # On read-only-workspace test runs, the fresh ephemeral tmp/home directories
    # start empty, cannot be moved into the workspace, and are deleted on exit, so
    # synthetic test fixtures (.git, .wisp, AGENTS.md, .env, settings.json) can
    # live there without weakening workspace or host protections.
    ephemeral_exception = (
        f"(require-any (subpath {quoted_tmp}) (subpath {_sbpl_string(str(home_dir))}))"
        if read_only_workspace
        else None
    )
    uv_cache = run_dir / "uv-cache"
    for name in _CONTROL_NAMES:
        regex = _sbpl_regex(_protected_regex(name))
        if name == ".git":
            # UV initializes this regular-file cache sentinel even with --no-sync.
            # UV numbers the bucket and renumbers it between releases. Only that
            # private-cache file is exempt, never a Git directory.
            marker = _sbpl_regex("^" + re.escape(str(uv_cache)) + r"/sdists-v[0-9]+/\.git$")
            git_exceptions = [f"(require-all (regex {marker}) (vnode-type REGULAR-FILE))"]
            if ephemeral_exception is not None:
                git_exceptions.append(ephemeral_exception)
            allowed = (
                git_exceptions[0]
                if len(git_exceptions) == 1
                else f"(require-any {' '.join(git_exceptions)})"
            )
            rules.append(
                f"(deny file-write* (require-all (regex {regex}) (require-not {allowed})))"
            )
            # A rename checks only the moved directory's own path, so a movable
            # cache or bucket would carry the exempt marker into the workspace.
            pinned = _sbpl_regex(
                "^" + _case_literal(str(uv_cache)) + "(/" + _case_literal("sdists-v") + "[0-9]+)?$"
            )
            rules.append(
                f"(deny file-write-unlink (require-all (vnode-type DIRECTORY) (regex {pinned})))"
            )
        elif ephemeral_exception is not None:
            rules.append(
                f"(deny file-write* (require-all (regex {regex}) "
                f"(require-not {ephemeral_exception})))"
            )
        else:
            rules.append(f"(deny file-write* (regex {regex}))")
    # Seatbelt evaluates ordered rules: restrictions must follow workspace grants.
    rules.extend(_package_protections(packages, workspace, ephemeral_exception=ephemeral_exception))
    # Scratch lives under ~/.wisp, but .wisp descendants of either writable root
    # are configuration, not scratch. Protect every nested project directory too.
    for root in (workspace,) if read_only_workspace else roots:
        regex = _sbpl_regex(
            "^" + _case_literal(str(root)) + "(/.*)?/" + _case_literal(".wisp") + "(/.*)?$"
        )
        rules.append(f"(deny file-read* file-write* (regex {regex}))")
    if read_only_workspace and cargo_target is not None:
        regex = _sbpl_regex(
            "^" + _case_literal(str(cargo_target)) + "(/.*)?/" + _case_literal(".wisp") + "(/.*)?$"
        )
        rules.append(f"(deny file-read* file-write* (regex {regex}))")
    patterns = (*protected_paths, ".env*", *_PRIVATE_NAMES, str(workspace / ".wisp"))
    expanded_patterns: list[str] = []
    bound_targets: set[Path] = set()
    for pattern in patterns:
        raw_regex = _protected_regex(pattern)
        expanded = _protected_pattern(pattern)
        expanded_patterns.append(expanded)
        if ephemeral_exception is not None and not expanded.startswith("/"):
            rules.append(
                f"(deny file-read* file-write* (require-all (regex {_sbpl_regex(raw_regex)}) "
                f"(require-not {ephemeral_exception})))"
            )
        else:
            rules.append(f"(deny file-read* file-write* (regex {_sbpl_regex(raw_regex)}))")
        if "*" not in expanded and "?" not in expanded:
            if "/" in expanded:
                # Also protect ancestors of suffix matches in nested directories and
                # scratch. Otherwise moving `sub/private` would expose its secret.
                for ancestor in Path(expanded).parents:
                    if ancestor in (Path("."), Path("/")):
                        continue
                    ancestor_regex = _sbpl_regex(_protected_regex(str(ancestor), subtree=False))
                    if ephemeral_exception is not None and not expanded.startswith("/"):
                        rules.append(
                            "(deny file-write-unlink (require-all (vnode-type DIRECTORY) "
                            f"(regex {ancestor_regex}) (require-not {ephemeral_exception})))"
                        )
                    else:
                        rules.append(
                            "(deny file-write-unlink (require-all (vnode-type DIRECTORY) "
                            f"(regex {ancestor_regex})))"
                        )
            path = Path(expanded).expanduser()
            path = path if path.is_absolute() else workspace / path
            if "/" in expanded or path.is_symlink():
                # Bind both names when an explicit protected path follows a symlink.
                _bind_symlink_target(
                    rules, path, seen=bound_targets, operations="file-read* file-write*"
                )
    _bind_workspace_symlinks(
        rules,
        workspace,
        protected_patterns=tuple(dict.fromkeys((*patterns, *expanded_patterns, "credentials"))),
        seen_protected=bound_targets,
        seen_control=set(),
    )
    return "\n".join(rules) + "\n"
