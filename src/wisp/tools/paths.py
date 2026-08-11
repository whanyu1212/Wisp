"""Path helpers for tools."""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError


def resolve_tool_path(
    path: str | None,
    context: ToolContext,
    *,
    default: str = ".",
    follow_leaf_symlink: bool = True,
) -> Path:
    """Resolve a tool path relative to the context working directory.

    Set ``follow_leaf_symlink=False`` for exclusive creation: parent directories
    are still resolved, but the final path component remains lexical so opening it
    with create-only semantics cannot follow a symlink introduced by a race.

    By default, tools are sandboxed to ``context.cwd``. Absolute paths and
    ``~`` are accepted only when they resolve back inside that directory.

    Paths matching the context's protected globs (secrets such as ``.env``) are
    rejected so their contents never reach the model, regardless of the sandbox.
    """

    selected = path if path is not None else default
    cwd = context.cwd.resolve(strict=False)
    candidate = Path(selected).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    resolved = candidate.resolve(strict=False)
    if not context.allow_outside_cwd:
        try:
            resolved.relative_to(cwd)
        except ValueError as exc:
            raise ToolError(f"Path is outside the tool working directory: {selected}") from exc
    # Check the lexical candidate (pre-symlink-dereference), not the resolved
    # target: a protected name such as ``.env`` must be denied even when it is a
    # symlink to an innocuously named file. is_protected_path also re-checks the
    # resolved target, so a link *to* a secret is caught as well.
    if is_protected_path(candidate, context):
        raise ToolError(f"Access to protected path denied: {selected}")
    if follow_leaf_symlink:
        return resolved
    return candidate.parent.resolve(strict=False) / candidate.name


def is_protected_path(path: Path, context: ToolContext) -> bool:
    """Return whether ``path`` matches any of the context's protected globs.

    Matching rules, all case-insensitive to avoid trivial bypasses on
    case-insensitive filesystems:

    - A **bare** pattern (no ``/``), like ``.env`` or ``*.key``, matches on the
      file's *basename* only. It protects the file at any depth but does not
      match a mere directory component (``docs/id_rsa/readme`` is not protected
      by ``id_rsa``).
    - A **path** pattern (contains ``/``), like ``.wisp/auth.json``, matches as a
      path suffix against both the cwd-relative path and the absolute path, so it
      keeps protecting the secret even when it is read from outside ``cwd`` (with
      ``allow_outside_cwd``). A leading ``**/`` is treated the same as a plain
      suffix match.

    The suffix semantics are what let a slash-bearing default protect
    ``/home/user/.wisp/auth.json`` regardless of the current working directory.

    Both the **lexical** path (as requested, without following symlinks) and the
    **symlink-resolved** target are checked, and either matching protects the
    path. This blocks a ``.env -> secret.txt`` symlink (lexical name matches) as
    well as an innocuously named link pointing *at* a secret (target matches).
    """

    patterns = context.protected_paths
    if not patterns:
        return False

    cwd = context.cwd.resolve(strict=False)
    # Lexical form: make absolute against cwd WITHOUT dereferencing symlinks, so a
    # protected name that links elsewhere is still matched by its requested name.
    lexical = path if path.is_absolute() else cwd / path
    resolved = path.resolve(strict=False)
    for candidate in (lexical, resolved):
        if _candidate_is_protected(candidate, patterns, cwd):
            return True
    return False


def _candidate_is_protected(path: Path, patterns: tuple[str, ...], cwd: Path) -> bool:
    """Match one concrete path against the protected patterns (see is_protected_path)."""

    name = path.name
    abs_text = PurePosixPath(path.as_posix()).as_posix()
    try:
        rel_text: str | None = path.relative_to(cwd).as_posix()
    except ValueError:
        rel_text = None  # outside cwd; rely on basename + absolute-suffix matching

    for pattern in patterns:
        normalized = pattern.replace("\\", "/").lstrip("/")
        if normalized.startswith("**/"):
            normalized = normalized[3:]

        if "/" not in normalized:
            if _fn(name, normalized):
                return True
            continue

        # Path pattern: match as a suffix of the relative or absolute path so it
        # anchors to a trailing path segment rather than the filesystem root.
        if rel_text is not None and _fn(rel_text, normalized):
            return True
        if _path_suffix_matches(abs_text, normalized):
            return True
    return False


def _path_suffix_matches(path_text: str, pattern: str) -> bool:
    """Return whether ``pattern`` matches ``path_text`` or any of its suffixes.

    ``a/b/c.json`` is tested against the pattern, then ``b/c.json``, then
    ``c.json`` — so ``b/c.json`` matches an absolute path ending in those
    segments. This gives slash-bearing patterns depth-independent reach.
    """

    parts = path_text.split("/")
    for start in range(len(parts)):
        suffix = "/".join(parts[start:])
        if _fn(suffix, pattern):
            return True
    return False


def _fn(value: str, pattern: str) -> bool:
    """Case-insensitive fnmatch."""

    return fnmatch(value.lower(), pattern.lower())


def display_tool_path(path: Path, context: ToolContext) -> str:
    """Return a stable, user-facing path for a tool result."""

    try:
        return str(path.resolve(strict=False).relative_to(context.cwd.resolve(strict=False)))
    except ValueError:
        return str(path)
