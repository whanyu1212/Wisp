"""Conservative command-review eligibility, not a shell sandbox or safe-prefix grant."""

from __future__ import annotations

import math
import os
import re
import shlex
import unicodedata
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from jsonschema import Draft202012Validator

from wisp.tools.context import ToolContext
from wisp.tools.files.paths import is_protected_path
from wisp.tools.shell.tool import BashTool
from wisp.trust.scope import is_broad_root

if TYPE_CHECKING:
    from wisp.events import ToolApprovalReviewed

MAX_REVIEW_COMMAND_CHARS = 4_000
MAX_REVIEW_REQUEST_CHARS = 12_000
_BASH_ARGUMENTS = Draft202012Validator(BashTool.input_schema)
_REVIEW_ARGUMENTS = frozenset(
    {"operation", "command", "timeout", "lifetime_seconds", "yield_seconds", "notify_on_exit"}
)
# Refuse composition even inside quotes. False negatives cost a prompt, not a grant.
_SHELL_SYNTAX = frozenset("|;&<>`$\\\n\r#*?[]{}~")
_PRIVATE_DIRECTORIES = frozenset(
    {".git", ".wisp", ".claude", ".codex", ".agents", ".ssh", ".aws", ".azure", ".gnupg"}
)
_PRIVATE_FILES = frozenset(
    {".npmrc", ".pypirc", ".git-credentials", "agents.md", "claude.md", "codex.md"}
)
_CREDENTIAL_OPTION = re.compile(r"password|token|secret|api[-_]?key|credential|authorization", re.I)
_RUNNERS = frozenset({"pytest", "ruff", "mypy"})
_SCRIPTS = frozenset(
    {"test", "build", "lint", "check", "typecheck", "format:check", "verify:browser"}
)
# These options can redirect execution, disclose private configuration, write to
# an unexpected destination, or turn an otherwise ordinary invocation into code.
_SENSITIVE_OPTIONS = frozenset(
    {
        "-c",
        "--config",
        "--config-file",
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--work-tree",
        "--output",
        "--output-file",
        "--prefix",
        "--global",
        "--script-shell",
        "--ext-diff",
        "--no-verify",
        "--eval",
        "--exec",
        "--execute",
        "--require",
        "--import",
        "--install-types",
        "--publish",
        "--upload",
    }
)


class CommandReviewer(Protocol):
    """Evaluate one exact proposal without running tools or granting future calls."""

    async def review(
        self,
        *,
        call_id: str,
        arguments: Mapping[str, object],
        context: ToolContext,
        still_authorized: Callable[[], bool],
    ) -> ToolApprovalReviewed:
        """Return an auditable Allow, Ask, or Deny for this call only."""
        ...


def command_review_objection(arguments: Mapping[str, object], context: ToolContext) -> str | None:
    """Return why a proposal needs human approval before any model review.

    Every eligible command still needs independent model review. Known test and
    build programs can execute arbitrary repository code; these checks do not
    establish filesystem/network containment or prove a program's behavior.

    Args:
        arguments (Mapping[str, object]): Exact built-in bash arguments.
        context (ToolContext): Working directory and existing path restrictions.

    Returns:
        str | None: A human-approval reason, or None when model review may proceed.
    """

    if os.name != "posix":
        return "Command review is supported only on macOS/Linux."
    if context.write_constraint is not None or context.atomic_writes_only:
        return "A restricted write context needs human approval for shell commands."
    if arguments.get("operation", "run") not in ("run", "start"):
        return "Process polling and cancellation retain their existing approval flow."
    if (
        set(arguments) - _REVIEW_ARGUMENTS
        or not _BASH_ARGUMENTS.is_valid(dict(arguments))
        or any(
            isinstance(value, float) and not math.isfinite(value) for value in arguments.values()
        )
    ):
        return "Only bounded, valid built-in command arguments can be reviewed."
    command = arguments.get("command")
    if (
        not isinstance(command, str)
        or not command.strip()
        or len(command) > MAX_REVIEW_COMMAND_CHARS
    ):
        return "The command is missing or too large to review completely."
    if any(char in _SHELL_SYNTAX or unicodedata.category(char) == "Cc" for char in command):
        return (
            "Shell composition, expansion, redirection, and control characters need human approval."
        )
    if any(
        unicodedata.bidirectional(char)
        in {"RLO", "LRO", "RLE", "LRE", "PDF", "RLI", "LRI", "FSI", "PDI"}
        for char in command
    ):
        return "Bidirectional control characters need human approval."
    try:
        cwd = context.cwd.resolve(strict=False)
        boundary = context.boundary()
        if is_broad_root(boundary) or not cwd.is_relative_to(boundary):
            return "Commands in broad or outside-workspace directories need human approval."
    except (OSError, ValueError, RuntimeError):
        return "The working directory could not be resolved; human approval required."
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return "The shell quoting is not complete enough to review."
    if not tokens:
        return "An empty command cannot be reviewed."
    if tokens[0] == "git" and any(":" in token for token in tokens[1:]):
        return "Git object/revision paths need human approval."
    try:
        objection = _argument_objection(tokens, context, cwd, boundary)
    except (OSError, ValueError, RuntimeError):
        return "A command path could not be resolved; human approval required."
    if objection is not None:
        return objection
    if not _development_command(tokens):
        return "This is not a supported routine development command; approve it manually."
    return None


def _argument_objection(
    tokens: list[str], context: ToolContext, cwd: Path, boundary: Path
) -> str | None:
    for token in tokens:
        if "://" in token or token.startswith("@"):
            return "Network destinations and response files need human approval."
        option = token.partition("=")[0]
        if (token.startswith("-") and _CREDENTIAL_OPTION.search(option)) or re.search(
            r"(?:password|token|secret|api[-_]?key|credential|authorization)[:=]", token, re.I
        ):
            return "Credential arguments need human approval and are not sent to the reviewer."
        if token.startswith("-") and not token.startswith("--") and len(token) > 2:
            return "Combined or attached short options need human approval."
        if option in _SENSITIVE_OPTIONS:
            return "Execution/configuration overrides and output redirection need human approval."
        value = token.partition("=")[2] if token.startswith("-") and "=" in token else token
        if value.startswith("-") or not value:
            continue
        # Test node selectors must not disguise the path they would load.
        path = Path(value.partition("::")[0])
        path = path if path.is_absolute() else cwd / path
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(boundary) or is_protected_path(path, context):
            return "Outside-workspace and protected paths need human approval."
        if (
            any(part.casefold() in _PRIVATE_DIRECTORIES for part in (*path.parts, *resolved.parts))
            or path.name.casefold() in _PRIVATE_FILES
            or resolved.name.casefold() in _PRIVATE_FILES
            or path.name.casefold().startswith(".env")
            or resolved.name.casefold().startswith(".env")
        ):
            return "Credential, Git metadata, and agent-control paths need human approval."
    return None


def _development_command(tokens: list[str]) -> bool:
    executable, *args = tokens
    if executable == "uv":
        # uv run may otherwise install/build dependencies before the reviewed program.
        if len(args) < 3 or args[:2] != ["run", "--no-sync"]:
            return False
        return _development_command(args[2:])
    if executable in {"python", "python3"}:
        return (
            len(args) >= 2 and args[0] == "-m" and args[1] in {"pytest", "ruff", "mypy", "unittest"}
        )
    if executable in _RUNNERS:
        return True
    if executable == "git":
        return bool(args) and args[0] in {"status", "diff", "log", "show", "ls-files", "rev-parse"}
    if executable == "cargo":
        return bool(args) and args[0] in {"test", "check", "build", "clippy", "fmt"}
    if executable in {"npm", "pnpm", "yarn"}:
        return bool(args) and (
            args[0] == "test" or (len(args) >= 2 and args[0] == "run" and args[1] in _SCRIPTS)
        )
    if executable == "go":
        return bool(args) and args[0] in {"test", "build", "vet"}
    return executable == "pwd" and not args
