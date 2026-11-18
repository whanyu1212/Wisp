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
import subprocess
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from wisp.tools.result import ToolError
from wisp.trust.scope import is_broad_root

if TYPE_CHECKING:
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
(deny system-fcntl (fcntl-command 80 110))
"""
# F_MAKECOMPRESSED/F_TRANSFEREXTENTS can mutate read-only descriptors. This
# explicit denial follows the finding documented by OpenAI Codex's Seatbelt tests.
_CONTROL_NAMES = (".git", ".codex", ".claude", ".agents", "AGENTS.md", "CLAUDE.md", "CODEX.md")
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
    "credentials",
    "credentials.json",
)


@dataclass(frozen=True, slots=True)
class SeatbeltLaunch:
    """Immutable authority bound to the exact command and working directory."""

    command: str
    cwd: Path
    profile: str
    environment: tuple[tuple[str, str], ...]
    roots: tuple[Path, ...]

    def validate(self, command: str, cwd: Path) -> None:
        """Reject rebinding or symlinked roots before spawning any command."""
        if command != self.command or cwd.resolve() != self.cwd:
            raise ToolError("Sandbox launch no longer matches the approved command.")
        for root in self.roots:
            _validate_root(root)

    def argv(self, wrapped_command: str, jobs_file: Path) -> list[str]:
        """Add only the host-owned job recorder to the prepared policy."""
        recorder = f"(allow file-write-data (literal {json.dumps(str(jobs_file.resolve()))}))"
        return [_EXECUTABLE, "-p", self.profile + recorder, "/bin/sh", "-c", wrapped_command]


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


def sandbox_development_command(arguments: Mapping[str, object]) -> bool:
    """Recognize the fast path after the shared command eligibility checks pass."""
    command = arguments.get("command")
    if not isinstance(command, str):
        return False
    tokens = shlex.split(command)
    if tokens[:2] == ["uv", "run"]:
        if tokens[2:3] != ["--no-sync"]:
            return False
        tokens = tokens[3:]
    if not tokens:
        return False
    if tokens[0] in {"python", "python3"}:
        return tokens[1:3] in (["-m", "pytest"], ["-m", "unittest"])
    if tokens[0] == "pytest":
        return True
    if tokens[0] == "cargo":
        return tokens[1:2] in (["test"], ["check"], ["build"])
    if tokens[0] == "npm":
        return tokens[1:2] == ["test"] or tokens[1:3] in (
            ["run", "test"],
            ["run", "build"],
        )
    return False


def _validate_root(root: Path) -> None:
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


def _protected_regex(pattern: str, cwd: Path) -> str:
    # Unsupported glob syntax is an admission failure, not an ignored protection.
    if any(char in pattern for char in "[]{}\\\n\r\0") or (
        "/" in pattern and any(char in pattern for char in "*?")
    ):
        raise ToolError("Sandbox cannot enforce this protected-path pattern.")
    expanded = os.path.expanduser(pattern)
    if expanded.startswith("/"):
        prefix = "^"
    elif "/" in expanded:
        expanded = str(cwd / expanded)
        prefix = "^"
    else:
        prefix = "^(.*/)?"
    parts = expanded.split("*")
    expression = "[^/]*".join(
        "[^/]".join(_case_literal(part) for part in piece.split("?")) for piece in parts
    )
    return prefix + expression + "(/.*)?$"


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
    _validate_root(context.workspace_root or context.cwd)
    if context.scratch_root is None:
        raise ToolError("Automatic commands require a private session scratch directory.")
    _validate_root(context.scratch_root)
    cwd = context.cwd.resolve()
    workspace = context.boundary()
    scratch = context.scratch_root.resolve()
    roots = (workspace, scratch)
    for root in roots:
        _validate_root(root)
    reads, environment = _runtime_access(roots)
    for key in context.withheld_environment:
        environment.pop(key, None)
    profile = _filesystem_profile(reads, roots, context.protected_paths)
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
        raise ToolError("Seatbelt capability probe failed; automatic execution stopped.") from exc
    if probe.returncode != 0:
        raise ToolError("Seatbelt policy could not be applied; automatic execution stopped.")
    return SeatbeltLaunch(command, cwd, profile, tuple(environment.items()), roots)


def _runtime_access(roots: tuple[Path, Path]) -> tuple[set[Path], dict[str, str]]:
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
            "/private/var/db/timezone",
            "/private/etc/localtime",
            "/private/etc/hosts",
            "/private/etc/passwd",
            "/private/etc/group",
            "/private/etc/ssl/openssl.cnf",
            "/dev/null",
            "/dev/zero",
            "/dev/random",
            "/dev/urandom",
            "/dev/fd",
        )
    }
    reads.update(roots)
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
    # Cache/home writes stay in the existing private session scratch. Programs may
    # create these directories; preparation itself does not mutate the workspace.
    environment = {
        "PATH": os.pathsep.join(dict.fromkeys(paths)),
        "HOME": str(scratch),
        "TMPDIR": str(scratch),
        "WISP_SCRATCH_DIR": str(scratch),
        "LANG": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "UV_CACHE_DIR": str(scratch / "uv-cache"),
        "npm_config_cache": str(scratch / "npm-cache"),
        "CARGO_HOME": str(home / ".cargo"),
        "RUSTUP_HOME": str(home / ".rustup"),
    }
    return reads, environment


def _filesystem_profile(
    reads: set[Path], roots: tuple[Path, Path], protected_paths: tuple[str, ...]
) -> str:
    """Compile read/write grants with final path and ancestor protections."""
    workspace, _ = roots
    rules = [_BASE]
    for root in sorted(reads):
        resolved_root = root.resolve()
        if is_broad_root(resolved_root):
            raise ToolError("A sandbox read root resolves to a broad directory.")
        rules.append(f"(allow file-read* (subpath {json.dumps(str(resolved_root))}))")
    for root in roots:
        quoted = json.dumps(str(root))
        rules.append(f"(allow file-write* (subpath {quoted}))")
        for ancestor in (root, *root.parents):
            rules.append(
                "(deny file-write-unlink (require-all (vnode-type DIRECTORY) "
                f"(literal {json.dumps(str(ancestor))})))"
            )
    for name in _CONTROL_NAMES:
        regex = _protected_regex(name, workspace).replace('"', '\\"')
        rules.append(f'(deny file-write* (regex #"{regex}"))')
    # Scratch lives under ~/.wisp, but .wisp descendants of either writable root
    # are configuration, not scratch. Protect every nested project directory too.
    for root in roots:
        regex = "^" + _case_literal(str(root)) + "(/.*)?/" + _case_literal(".wisp") + "(/.*)?$"
        regex = regex.replace('"', '\\"')
        rules.append(f'(deny file-read* file-write* (regex #"{regex}"))')
    patterns = (*protected_paths, ".env*", *_PRIVATE_NAMES, str(workspace / ".wisp"))
    for pattern in patterns:
        # SBPL's #"..." is a raw regex literal, unlike its ordinary path strings.
        regex = _protected_regex(pattern, workspace).replace('"', '\\"')
        rules.append(f'(deny file-read* file-write* (regex #"{regex}"))')
        if "/" in pattern and "*" not in pattern and "?" not in pattern:
            path = Path(pattern).expanduser()
            path = path if path.is_absolute() else workspace / path
            # Bind both names when an explicit protected path currently follows a symlink.
            resolved = path.resolve()
            if resolved != path:
                rules.append(f"(deny file-read* file-write* (subpath {json.dumps(str(resolved))}))")
            for protected in (path, resolved):
                for ancestor in protected.parents:
                    rules.append(
                        "(deny file-write-unlink (require-all (vnode-type DIRECTORY) "
                        f"(literal {json.dumps(str(ancestor))})))"
                    )
    return "\n".join(rules) + "\n"
