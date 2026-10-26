"""Path helpers for tools."""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from fnmatch import translate
from functools import lru_cache
from pathlib import Path

from wisp.tools.context import ToolContext


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
    if matches_protected_pattern(lexical, patterns):
        return True

    # os.path.realpath, unlike Path.resolve on Python 3.12, leaves a symlink loop in
    # place instead of raising; opening the path then fails cleanly.
    resolved = Path(os.path.realpath(lexical))
    # Keep the target check for symlinks, but avoid matching the same path twice.
    return resolved != lexical and matches_protected_pattern(resolved, patterns)


def matches_protected_pattern(path: Path, patterns: tuple[str, ...]) -> bool:
    """Return whether an absolute path, taken as written, matches a protected glob.

    Unlike ``is_protected_path``, this does not resolve symbolic links. Callers
    that already know the real path, such as a walk that never follows links,
    use it to avoid resolving every file.

    Args:
        path (Path): Absolute path to match.
        patterns (tuple[str, ...]): Protected globs, as in ``ToolContext``.

    Returns:
        bool: Whether a bare pattern matches the basename, or a path pattern
            matches a trailing run of the path's segments.
    """

    if not patterns:
        return False
    matcher = _protected_matcher(tuple(patterns))
    text = _caseless(path.as_posix())
    if matcher.name is not None and matcher.name.match(text.rsplit("/", 1)[-1]):
        return True
    return matcher.path is not None and matcher.path.match(text) is not None


def _caseless(text: str) -> str:
    # Canonical caseless matching (Unicode 3.13). Case-insensitive filesystems fold
    # more than str.lower(): APFS opens "credentialſ.json" as "credentials.json",
    # and it treats composed and decomposed characters as the same name.
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", text).casefold())


@dataclass(frozen=True, slots=True)
class _ProtectedMatcher:
    name: re.Pattern[str] | None
    path: re.Pattern[str] | None


@lru_cache(maxsize=16)
def _protected_matcher(patterns: tuple[str, ...]) -> _ProtectedMatcher:
    name_patterns: list[str] = []
    path_patterns: list[str] = []
    for pattern in patterns:
        normalized = _caseless(pattern.replace("\\", "/").lstrip("/"))
        if normalized.startswith("**/"):
            normalized = normalized[3:]
        if "/" in normalized:
            path_patterns.append(translate(normalized))
        else:
            name_patterns.append(translate(normalized))
    return _ProtectedMatcher(
        name=re.compile("|".join(name_patterns)) if name_patterns else None,
        # A path pattern matches the whole path or any suffix that starts after a
        # "/", so it anchors to trailing segments wherever the path is rooted. That
        # covers the cwd-relative path, which is itself such a suffix.
        path=re.compile(f"(?s:.*/)?(?:{'|'.join(path_patterns)})") if path_patterns else None,
    )


def display_tool_path(path: Path, context: ToolContext) -> str:
    """Return a stable, user-facing path for a tool result."""

    try:
        return str(path.resolve(strict=False).relative_to(context.cwd.resolve(strict=False)))
    except ValueError:
        return str(path)
