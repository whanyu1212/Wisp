"""Secure, bounded loading of one discovered Agent Skill resource."""

from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from wisp.skills.discovery import MAX_FRONTMATTER_BYTES
from wisp.skills.filesystem import (
    DIRECTORY_FLAGS,
    FILE_FLAGS,
    PATH_FALLBACK_SUPPORTED,
    USE_DESCRIPTOR_TRAVERSAL,
    close_windows_handle,
    is_link_like,
    open_path_directory_guard,
    open_path_file,
    open_relative,
    resolved_open_file,
)
from wisp.skills.models import SkillEntry
from wisp.tools.common import _truncate_text
from wisp.tools.context import ToolContext
from wisp.tools.paths import is_protected_path
from wisp.tools.result import ToolError


@dataclass(frozen=True, slots=True)
class SkillResource:
    """Bounded text returned from one skill-relative resource."""

    text: str
    resource: str
    truncated: bool = False


def load_skill_resource(
    entry: SkillEntry,
    resource: str | None,
    *,
    context: ToolContext,
) -> SkillResource:
    """Load one UTF-8 resource without allowing links or root escape."""

    normalized = _normalize_resource(resource)
    candidate = entry.root.joinpath(*PurePosixPath(normalized).parts)
    try:
        if is_protected_path(candidate, context):
            raise ToolError(f"Skill resource is protected: {normalized}")

        if USE_DESCRIPTOR_TRAVERSAL:
            file_fd = _open_resource_by_descriptor(entry.root, normalized)
            resolved = candidate
            guards: tuple[int, ...] = ()
        elif PATH_FALLBACK_SUPPORTED:
            file_fd, resolved, guards = _open_resource_by_path(entry.root, normalized)
        else:
            raise ToolError("Secure skill resource loading is unavailable on this platform")
    except ToolError:
        raise
    except (OSError, RuntimeError) as exc:
        detail = exc.strerror if isinstance(exc, OSError) else type(exc).__name__
        raise ToolError(
            f"Cannot open skill resource {normalized}: {detail or type(exc).__name__}"
        ) from None

    try:
        if is_protected_path(resolved, context):
            raise ToolError(f"Skill resource is protected: {normalized}")
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise ToolError(f"Skill resource is not a regular file: {normalized}")
        if normalized == "SKILL.md":
            _skip_frontmatter(file_fd)
        text, stream_truncated = _read_bounded_text(
            file_fd,
            max_bytes=max(0, context.max_output_bytes),
            max_lines=max(0, context.max_output_lines),
            resource=normalized,
        )
        bounded = _truncate_text(text, context=context, force_truncated=stream_truncated)
        return SkillResource(text=bounded.text, resource=normalized, truncated=bounded.truncated)
    finally:
        os.close(file_fd)
        for guard in guards:
            close_windows_handle(guard)


def _normalize_resource(resource: str | None) -> str:
    selected = "SKILL.md" if resource is None else resource
    if type(selected) is not str or not selected:
        raise ToolError("skill.resource must be a non-empty relative path")
    if "\\" in selected:
        raise ToolError("skill.resource must use forward slashes")
    if (
        selected.startswith("/")
        or selected.endswith("/")
        or any(component in {"", ".", ".."} for component in selected.split("/"))
    ):
        raise ToolError("skill.resource must stay within the selected skill")
    path = PurePosixPath(selected)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ToolError("skill.resource must stay within the selected skill")
    return path.as_posix()


def _open_resource_by_descriptor(root: Path, resource: str) -> int:
    current_fd = _open_canonical_directory(root)
    current_path = root
    parts = PurePosixPath(resource).parts
    try:
        for component in parts[:-1]:
            next_fd = open_relative(
                component,
                DIRECTORY_FLAGS,
                directory_fd=current_fd,
                directory_path=current_path,
            )
            os.close(current_fd)
            current_fd = next_fd
            current_path /= component
        return open_relative(
            parts[-1],
            FILE_FLAGS,
            directory_fd=current_fd,
            directory_path=current_path,
        )
    except OSError as exc:
        raise _resource_open_error(resource, exc) from exc
    finally:
        os.close(current_fd)


def _open_canonical_directory(path: Path) -> int:
    current_path = Path(path.anchor)
    current_fd = os.open(current_path, DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            next_fd = open_relative(
                component,
                DIRECTORY_FLAGS,
                directory_fd=current_fd,
                directory_path=current_path,
            )
            os.close(current_fd)
            current_fd = next_fd
            current_path /= component
    except BaseException:
        os.close(current_fd)
        raise
    return current_fd


def _open_resource_by_path(root: Path, resource: str) -> tuple[int, Path, tuple[int, ...]]:
    guards: list[int] = []
    current = root
    try:
        for component in PurePosixPath(resource).parts[:-1]:
            current /= component
            if is_link_like(current):
                raise ToolError(f"Skill resource path contains a link: {resource}")
            guard = open_path_directory_guard(current)
            if guard is not None:
                guards.append(guard)
        candidate = root.joinpath(*PurePosixPath(resource).parts)
        if is_link_like(candidate):
            raise ToolError(f"Skill resource must not be a link: {resource}")
        file_fd = open_path_file(candidate)
        resolved = resolved_open_file(file_fd, path=candidate)
        try:
            resolved.relative_to(root)
        except ValueError:
            os.close(file_fd)
            raise ToolError(f"Skill resource resolves outside its root: {resource}") from None
        return file_fd, resolved, tuple(guards)
    except BaseException:
        for guard in guards:
            close_windows_handle(guard)
        raise


def _resource_open_error(resource: str, exc: OSError) -> ToolError:
    if isinstance(exc, FileNotFoundError):
        return ToolError(f"Skill resource does not exist: {resource}")
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        return ToolError(f"Skill resource path contains a link or non-directory: {resource}")
    return ToolError(f"Cannot open skill resource {resource}: {exc.strerror or type(exc).__name__}")


def _skip_frontmatter(file_fd: int) -> None:
    consumed = 0
    with os.fdopen(os.dup(file_fd), "rb", buffering=0) as stream:
        opening = stream.readline(MAX_FRONTMATTER_BYTES + 1)
        consumed += len(opening)
        if opening not in {b"---\n", b"---\r\n"}:
            raise ToolError("SKILL.md no longer has valid YAML frontmatter")
        while True:
            remaining = MAX_FRONTMATTER_BYTES - consumed
            line = stream.readline(max(0, remaining) + 1)
            consumed += len(line)
            if consumed > MAX_FRONTMATTER_BYTES or not line:
                raise ToolError("SKILL.md no longer has bounded YAML frontmatter")
            if line in {b"---\n", b"---\r\n", b"---"}:
                return


def _read_bounded_text(
    file_fd: int,
    *,
    max_bytes: int,
    max_lines: int,
    resource: str,
) -> tuple[str, bool]:
    if max_bytes == 0 or max_lines == 0:
        return "", True
    parts: list[bytes] = []
    byte_count = 0
    line_count = 0
    truncated = False
    with os.fdopen(os.dup(file_fd), "rb", buffering=0) as stream:
        while line_count < max_lines and byte_count < max_bytes:
            remaining = max_bytes - byte_count
            line = stream.readline(remaining + 1)
            if not line:
                break
            if len(line) > remaining:
                parts.append(line[:remaining])
                truncated = True
                break
            parts.append(line)
            byte_count += len(line)
            line_count += 1
        if not truncated and stream.read(1):
            truncated = True
    raw = b"".join(parts)
    try:
        return raw.decode("utf-8"), truncated
    except UnicodeDecodeError as exc:
        if truncated and exc.end == len(raw) and exc.reason == "unexpected end of data":
            return raw[: exc.start].decode("utf-8"), True
        raise ToolError(f"Skill resource is not valid UTF-8: {resource}") from exc


__all__ = ["SkillResource", "load_skill_resource"]
