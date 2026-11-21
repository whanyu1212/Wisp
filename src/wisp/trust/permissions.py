"""User-owned permission defaults, isolated by canonical project directory.

Elevated modes are never kept for a broad folder such as the home folder (see
:mod:`wisp.trust.scope`): saving either there is refused, and records already
saved there are ignored. Elevated modes there last one session at most.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import tempfile
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wisp.trust.scope import BROAD_FOLDER_DESCRIPTION, is_broad_root

if TYPE_CHECKING:
    # Type-only: ``wisp.config`` loads this module while ``wisp.events`` may still be
    # initializing, so the runtime import graph must not point back at events.
    from wisp.events import PermissionMode

MAX_COMMAND_ALLOW_RULES = 64
MAX_COMMAND_PREFIX_CHARS = 128
MAX_COMMAND_PREFIX_TOKENS = 4
_MAX_MATCH_COMMAND_CHARS = 4_000
_PREFERENCE_FILE_MAX_BYTES = 16_384

# Refuse shell composition, redirection, expansion, comments, escapes, and control characters
# even inside quotes so a prefix grant can never approve a compound or dynamic shell command.
_DISALLOWED_SHELL_CHARS = frozenset("|;&<>`$()\\\n\r#")
_BIDI_CATEGORIES = frozenset({"RLO", "LRO", "RLE", "LRE", "PDF", "RLI", "LRI", "FSI", "PDI"})
# POSIX shell reserved words and shell builtins that alter control flow, environment, or
# command execution rather than running a standalone tool directly.
_SHELL_RESERVED_WORDS = frozenset(
    {
        "!",
        ":",
        "[",
        "[[",
        "]",
        "]]",
        "{",
        "}",
        "alias",
        "bg",
        "case",
        "cd",
        "chdir",
        "coproc",
        "declare",
        "dirs",
        "disown",
        "do",
        "done",
        "elif",
        "else",
        "enable",
        "esac",
        "export",
        "fc",
        "fg",
        "fi",
        "for",
        "function",
        "hash",
        "history",
        "if",
        "in",
        "jobs",
        "let",
        "local",
        "popd",
        "pushd",
        "read",
        "readonly",
        "return",
        "select",
        "set",
        "shift",
        "shopt",
        "then",
        "trap",
        "typeset",
        "ulimit",
        "umask",
        "unalias",
        "unset",
        "until",
        "wait",
        "while",
    }
)
# Shells, privilege/exec wrappers, and destructive/network primitives are never offered as
# reusable prefix rules.
_DISALLOWED_EXECUTABLES = frozenset(
    {
        ".",
        "ash",
        "bash",
        "builtin",
        "chmod",
        "chown",
        "command",
        "csh",
        "curl",
        "dash",
        "dd",
        "doas",
        "env",
        "eval",
        "exec",
        "fish",
        "kill",
        "killall",
        "ksh",
        "ltrace",
        "mkfs",
        "nc",
        "ncat",
        "nice",
        "nohup",
        "pkill",
        "rm",
        "rsync",
        "scp",
        "sh",
        "source",
        "ssh",
        "strace",
        "su",
        "sudo",
        "tcsh",
        "time",
        "timeout",
        "wget",
        "xargs",
        "zsh",
    }
)
# Interpreters that may only be prefixed via a specific module invocation (`python -m <mod>`).
_MODULE_ONLY_INTERPRETERS = frozenset({"python", "python3"})
# Multi-subcommand tools whose prefix includes the subcommand (`cargo test`, `git status`).
_SUBCOMMAND_TOOLS = frozenset(
    {
        "cargo",
        "devin",
        "docker",
        "gh",
        "git",
        "go",
        "just",
        "kubectl",
        "make",
        "mise",
        "npm",
        "pip",
        "pip3",
        "pnpm",
        "poetry",
        "ruff",
        "uv",
        "wisp",
        "yarn",
    }
)


def permissions_directory() -> Path:
    """Return the user-local directory reserved for permission preferences."""

    return Path.home() / ".wisp" / "permissions"


def _project_key(project: Path) -> str:
    return str(project.expanduser().resolve(strict=False))


def _preference_path(project: Path) -> Path:
    digest = hashlib.sha256(_project_key(project).encode()).hexdigest()
    return permissions_directory() / f"{digest}.json"


def _private_directory(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISDIR(info.st_mode) and (
        os.name != "posix" or (info.st_uid == os.getuid() and info.st_mode & 0o022 == 0)
    )


class BroadRootYoloError(PermissionError):
    """An elevated mode cannot be saved for a broad folder such as home."""


def load_permission_mode(project: Path) -> PermissionMode | None:
    """Read this project's saved mode, failing closed on invalid or unsafe files.

    Args:
        project (Path): Project directory; aliases resolve to the same preference.

    Returns:
        PermissionMode | None: Explicit saved mode, or None when no usable record exists.
            Callers must use approval prompts when there is no saved mode. A saved
            elevated mode for a broad folder is ignored and reads as None.
    """

    mode = _read_saved_mode(project)
    if mode in ("auto", "auto_commands", "yolo") and is_broad_root(project):
        return None
    return mode


def ignored_broad_root_yolo(project: Path) -> bool:
    """Return whether a YOLO record saved for a broad folder is being ignored.

    Older releases could save YOLO for the home folder. Callers use this to tell
    the user once that it no longer applies.

    Args:
        project (Path): Project directory.

    Returns:
        bool: True when ``project`` is broad and has a saved YOLO record.
    """

    return is_broad_root(project) and _read_saved_mode(project) == "yolo"


def _read_preference_record(project: Path) -> dict[str, Any] | None:
    path = _preference_path(project)
    try:
        if not _private_directory(path.parent) or path.is_symlink():
            return None
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        with os.fdopen(fd, encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > _PREFERENCE_FILE_MAX_BYTES:
                return None
            if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
                return None
            record = json.loads(stream.read(_PREFERENCE_FILE_MAX_BYTES + 1))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    if not isinstance(record, dict) or record.get("project") != _project_key(project):
        return None
    return record


def _mode_from_record(record: dict[str, Any]) -> PermissionMode | None:
    mode = record.get("mode")
    if mode == "ask":
        return "ask"
    # Shell approval must never reuse this edit-only consent in a future release.
    if mode == "auto" and record.get("auto_scope") == "workspace_edit_v1":
        return "auto"
    if mode == "auto_commands" and record.get("auto_scope") == "sandbox_commands_v1":
        return "auto_commands"
    if mode == "yolo":
        return "yolo"
    return None


def _read_saved_mode(project: Path) -> PermissionMode | None:
    record = _read_preference_record(project)
    return _mode_from_record(record) if record is not None else None


def _rules_from_record(record: dict[str, Any]) -> tuple[str, ...]:
    raw = record.get("command_allow_rules")
    if not isinstance(raw, list):
        return ()
    valid: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        tokens = validate_command_prefix(item)
        if tokens is None:
            continue
        canonical = " ".join(tokens)
        if canonical not in seen:
            seen.add(canonical)
            valid.append(canonical)
            if len(valid) == MAX_COMMAND_ALLOW_RULES:
                break
    return tuple(valid)


def _write_preference_record(
    project: Path,
    *,
    mode: PermissionMode | None,
    command_allow_rules: tuple[str, ...],
) -> None:
    path = _preference_path(project)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not _private_directory(path.parent):
        raise PermissionError("Permission preferences require a user-owned directory")
    record: dict[str, Any] = {"project": _project_key(project)}
    if mode is not None:
        record["mode"] = mode
        if mode == "auto":
            record["auto_scope"] = "workspace_edit_v1"
        elif mode == "auto_commands":
            record["auto_scope"] = "sandbox_commands_v1"
    if command_allow_rules:
        record["command_allow_rules"] = list(command_allow_rules)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = stream.name
            json.dump(record, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def parse_simple_command(command: str) -> tuple[str, ...] | None:
    """Split a simple POSIX shell command into argv tokens, failing closed on shell syntax.

    Refuses pipelines, chaining, redirections, subshells, variable expansion, comments,
    backslash escapes, control/bidi characters, leading env-var assignments, and unclosed
    quotes so prefix rules only ever match a single direct command invocation.

    Args:
        command (str): Raw shell command string.

    Returns:
        tuple[str, ...] | None: Parsed argv tokens, or None if the command is not a simple
            single command eligible for prefix matching.
    """

    if not command or not command.strip() or len(command) > _MAX_MATCH_COMMAND_CHARS:
        return None
    if any(
        ch in _DISALLOWED_SHELL_CHARS
        or unicodedata.category(ch) == "Cc"
        or unicodedata.bidirectional(ch) in _BIDI_CATEGORIES
        for ch in command
    ):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    executable = tokens[0]
    # Reject leading env assignments (`FOO=bar pytest`), flags, shell reserved words/operators
    # (`! pytest`, `if ...`), and disallowed wrappers.
    if "=" in executable or executable.startswith("-"):
        return None
    base_name = Path(executable).name.casefold()
    if (
        not base_name
        or not (base_name[0].isascii() and base_name[0].isalnum())
        or base_name in _DISALLOWED_EXECUTABLES
        or base_name in _SHELL_RESERVED_WORDS
    ):
        return None
    return tuple(tokens)


def _is_safe_prefix_token(token: str) -> bool:
    return (
        bool(token)
        and token.isascii()
        and all(0x21 <= ord(ch) <= 0x7E and ch not in _DISALLOWED_SHELL_CHARS for ch in token)
        and "=" not in token
    )


def validate_command_prefix(prefix: str) -> tuple[str, ...] | None:
    """Validate and canonicalize a saved command-prefix rule.

    Args:
        prefix (str): Space-separated prefix rule (for example ``"cargo test"``).

    Returns:
        tuple[str, ...] | None: Canonical prefix tokens, or None if invalid or unsafe.
    """

    if not prefix or len(prefix) > MAX_COMMAND_PREFIX_CHARS:
        return None
    if any(
        ch in _DISALLOWED_SHELL_CHARS
        or unicodedata.category(ch) == "Cc"
        or unicodedata.bidirectional(ch) in _BIDI_CATEGORIES
        for ch in prefix
    ):
        return None
    tokens = tuple(prefix.split())
    if not (1 <= len(tokens) <= MAX_COMMAND_PREFIX_TOKENS):
        return None
    if not all(_is_safe_prefix_token(token) for token in tokens):
        return None
    executable = tokens[0]
    if "/" in executable or executable.startswith("-"):
        return None
    base_name = executable.casefold()
    if (
        not (base_name[0].isascii() and base_name[0].isalnum())
        or base_name in _DISALLOWED_EXECUTABLES
        or base_name in _SHELL_RESERVED_WORDS
    ):
        return None
    if base_name in _MODULE_ONLY_INTERPRETERS:
        if len(tokens) < 3 or tokens[1] != "-m" or tokens[2].startswith("-"):
            return None
    elif base_name in _SUBCOMMAND_TOOLS:
        if len(tokens) < 2 or tokens[1].startswith("-") or "/" in tokens[1]:
            return None
        if base_name in {"uv", "npm", "pnpm", "yarn"} and tokens[1] == "run":
            if len(tokens) < 3 or tokens[2].startswith("-") or "/" in tokens[2]:
                return None
    if len(" ".join(tokens)) > MAX_COMMAND_PREFIX_CHARS:
        return None
    return tokens


def suggest_command_prefix(command: str) -> str | None:
    """Return a safe reusable prefix for ``command``, or None when it should not be saved.

    Args:
        command (str): Raw shell command proposed to the ``bash`` tool.

    Returns:
        str | None: Canonical prefix such as ``"uv run pytest"`` or ``"cargo test"``, or
            None if the command uses shell composition, env prefixes, disallowed tools,
            or cannot form a safe prefix.
    """

    tokens = parse_simple_command(command)
    if tokens is None:
        return None
    executable = tokens[0]
    if "/" in executable:
        return None
    base_name = executable.casefold()
    candidate: tuple[str, ...]
    if base_name in _MODULE_ONLY_INTERPRETERS:
        if len(tokens) < 3 or tokens[1] != "-m":
            return None
        candidate = tokens[:3]
    elif base_name in {"uv", "npm", "pnpm", "yarn"} and len(tokens) >= 3 and tokens[1] == "run":
        candidate = tokens[:3]
    elif base_name in _SUBCOMMAND_TOOLS:
        if len(tokens) < 2:
            return None
        candidate = tokens[:2]
    else:
        candidate = tokens[:1]
    validated = validate_command_prefix(" ".join(candidate))
    return " ".join(validated) if validated is not None else None


def command_matches_allow_rule(command: str, rule: str) -> bool:
    """Return whether ``command`` is a simple command matching saved prefix ``rule``.

    Matching is strictly token-based after rejecting shell metacharacters, quoting errors,
    and env-var prefixes, so ``"cargo test"`` never matches ``"cargo testing"``,
    ``"cargo test && rm -rf /"``, ``"FOO=1 cargo test"``, or ``"cargo test $(id)"``.

    Args:
        command (str): Proposed command string.
        rule (str): Saved command-prefix rule.

    Returns:
        bool: True only when both rule and command parse cleanly and ``command`` starts
            with ``rule``'s exact leading argv tokens.
    """

    rule_tokens = validate_command_prefix(rule)
    if rule_tokens is None:
        return False
    cmd_tokens = parse_simple_command(command)
    if cmd_tokens is None or len(cmd_tokens) < len(rule_tokens):
        return False
    return cmd_tokens[: len(rule_tokens)] == rule_tokens


def load_command_allow_rules(project: Path) -> tuple[str, ...]:
    """Read this project's saved command-prefix allow rules, failing closed on errors.

    Broad folders (such as home) never load saved command allow rules.

    Args:
        project (Path): Project directory.

    Returns:
        tuple[str, ...]: Validated canonical prefix rules, oldest first.
    """

    if is_broad_root(project):
        return ()
    record = _read_preference_record(project)
    return _rules_from_record(record) if record is not None else ()


def save_command_allow_rule(project: Path, prefix: str) -> tuple[str, ...]:
    """Persist one validated command-prefix allow rule for ``project``.

    Args:
        project (Path): Project directory.
        prefix (str): Command prefix to allow in future runs.

    Returns:
        tuple[str, ...]: Updated list of saved command allow rules for ``project``.

    Raises:
        BroadRootYoloError: ``project`` is a broad folder such as home.
        ValueError: ``prefix`` is invalid or the project has reached the rule limit.
        OSError: The preference file could not be written securely.
    """

    if is_broad_root(project):
        raise BroadRootYoloError(
            f"Command allow rules are not saved for {project}: it is {BROAD_FOLDER_DESCRIPTION}"
        )
    tokens = validate_command_prefix(prefix)
    if tokens is None:
        raise ValueError(f"Unsupported command allow prefix: {prefix!r}")
    canonical = " ".join(tokens)
    existing_record = _read_preference_record(project)
    existing_mode = _mode_from_record(existing_record) if existing_record is not None else None
    existing_rules = _rules_from_record(existing_record) if existing_record is not None else ()
    if canonical in existing_rules:
        return existing_rules
    if len(existing_rules) >= MAX_COMMAND_ALLOW_RULES:
        raise ValueError(f"Project already has {MAX_COMMAND_ALLOW_RULES} saved command allow rules")
    updated = (*existing_rules, canonical)
    _write_preference_record(project, mode=existing_mode, command_allow_rules=updated)
    return updated


def remove_command_allow_rule(project: Path, prefix: str) -> tuple[str, ...]:
    """Remove one saved command-prefix allow rule for ``project``.

    Args:
        project (Path): Project directory.
        prefix (str): Saved command prefix to revoke.

    Returns:
        tuple[str, ...]: Remaining saved command allow rules for ``project``.

    Raises:
        OSError: The updated preference file could not be written securely.
    """

    existing_record = _read_preference_record(project)
    existing_mode = _mode_from_record(existing_record) if existing_record is not None else None
    existing_rules = _rules_from_record(existing_record) if existing_record is not None else ()
    updated = tuple(rule for rule in existing_rules if rule != prefix)
    if updated == existing_rules:
        return existing_rules
    _write_preference_record(project, mode=existing_mode, command_allow_rules=updated)
    return updated


def save_permission_mode(project: Path, mode: PermissionMode) -> None:
    """Atomically save a permission default in the user-owned project record.

    Separate files prevent concurrent choices in different projects from overwriting
    each other. Nothing is read from or written to repository-local settings.

    Args:
        project (Path): Project directory whose future sessions use this default.
        mode (PermissionMode): Explicit user-selected default.

    Raises:
        ValueError: The mode is unsupported.
        BroadRootYoloError: ``mode`` is elevated and the project is broad.
        OSError: The preference cannot be saved securely. Callers must not claim or
            apply a persistent permission change when this write fails.
    """

    if mode not in ("ask", "auto", "auto_commands", "yolo"):
        raise ValueError(f"Unsupported permission mode: {mode!r}")
    if mode in ("auto", "auto_commands", "yolo") and is_broad_root(project):
        raise BroadRootYoloError(
            f"{mode.upper()} is not saved for {project}: it is {BROAD_FOLDER_DESCRIPTION}"
        )
    existing_record = _read_preference_record(project)
    existing_rules = (
        ()
        if is_broad_root(project) or existing_record is None
        else _rules_from_record(existing_record)
    )
    _write_preference_record(project, mode=mode, command_allow_rules=existing_rules)
