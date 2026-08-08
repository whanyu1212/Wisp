"""Strict, bounded discovery of Agent Skills metadata."""

from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wisp.skills import filesystem as skill_filesystem
from wisp.skills.metadata import SkillMetadataError, read_skill_metadata
from wisp.skills.models import (
    SkillCatalog,
    SkillDiagnostic,
    SkillDiagnosticCode,
    SkillDiagnosticSeverity,
    SkillEntry,
    SkillSource,
)
from wisp.tools.context import ToolContext
from wisp.tools.paths import is_protected_path

MAX_FRONTMATTER_BYTES = 16 * 1024
MAX_ROOT_ENTRIES = 256
MAX_ROOT_FRONTMATTER_BYTES = 256 * 1024
MAX_CATALOG_SKILLS = 256

# Keep local aliases so discovery's platform-fallback tests can replace one primitive
# without affecting progressive resource loading, which shares the implementations.
_close_windows_handle = skill_filesystem.close_windows_handle
_is_link_like = skill_filesystem.is_link_like
_shared_open_path_file = skill_filesystem.open_path_file
_open_relative = skill_filesystem.open_relative
_open_windows_directory_guard = skill_filesystem.open_windows_directory_guard
_open_windows_metadata_handle = skill_filesystem.open_windows_file_handle
_resolved_open_file = skill_filesystem.resolved_open_file
_resolved_windows_handle = skill_filesystem.resolved_windows_handle
_windows_handle_is_reparse_point = skill_filesystem.windows_handle_is_reparse_point
_windows_handle_to_fd = skill_filesystem.windows_handle_to_fd

_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd
_SCANDIR_SUPPORTS_FD = os.scandir in os.supports_fd
_USE_DESCRIPTOR_TRAVERSAL = (
    os.name != "nt"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and _OPEN_SUPPORTS_DIR_FD
    and _SCANDIR_SUPPORTS_FD
)
_PATH_FALLBACK_SUPPORTED = os.name == "nt"


@dataclass(frozen=True, slots=True)
class _SkillRoot:
    source: SkillSource
    base: Path
    components: tuple[str, str]

    @property
    def path(self) -> Path:
        return self.base.joinpath(*self.components)


@dataclass(frozen=True, slots=True)
class _RootScan:
    entries: tuple[SkillEntry, ...] = ()
    diagnostics: tuple[SkillDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class _OpenedRoot:
    fd: int | None
    guards: tuple[int, ...] = ()


class _RootLimitError(ValueError):
    pass


def discover_skills(
    *,
    home_dir: Path,
    project_root: Path | None,
    protected_paths: tuple[str, ...],
) -> SkillCatalog:
    """Discover one immutable skill catalog from allowed user and project roots.

    ``project_root=None`` is the trust boundary: project skill paths are not even
    constructed, much less inspected, until the caller has resolved project trust.
    """

    roots: list[_SkillRoot] = []
    diagnostics: list[SkillDiagnostic] = []
    project: Path | None = None
    project_specs: tuple[tuple[SkillSource, tuple[str, str]], ...] = (
        ("project:wisp", (".wisp", "skills")),
        ("project:agents", (".agents", "skills")),
    )
    if project_root is not None:
        project, project_diagnostics = _resolve_source_base(project_root, project_specs)
        diagnostics.extend(project_diagnostics)

    home_specs: tuple[tuple[SkillSource, tuple[str, str]], ...] = (
        ("user:wisp", (".wisp", "skills")),
        ("user:agents", (".agents", "skills")),
    )
    home, home_diagnostics = _resolve_source_base(home_dir, home_specs)
    diagnostics.extend(home_diagnostics)
    if project is not None and project != home:
        roots.extend(
            _SkillRoot(source, project, components) for source, components in project_specs
        )
    if home is not None:
        roots.extend(_SkillRoot(source, home, components) for source, components in home_specs)

    context = ToolContext(
        cwd=project or home or home_dir,
        protected_paths=protected_paths,
    )
    selected: dict[str, SkillEntry] = {}
    catalog_limit_reported = False
    for root in roots:
        scan = _scan_root(root, context=context)
        diagnostics.extend(scan.diagnostics)
        for entry in scan.entries:
            winner = selected.get(entry.name)
            if winner is not None:
                diagnostics.append(
                    _diagnostic(
                        "shadowed",
                        "warning",
                        (
                            f"skill {entry.name!r} from {entry.source} is shadowed by "
                            f"the higher-precedence {winner.source} skill"
                        ),
                        root.source,
                        entry.root,
                    )
                )
                continue
            if len(selected) >= MAX_CATALOG_SKILLS:
                if not catalog_limit_reported:
                    diagnostics.append(
                        _diagnostic(
                            "catalog-limit",
                            "error",
                            f"skill catalog exceeds the {MAX_CATALOG_SKILLS}-skill limit",
                            root.source,
                            root.path,
                        )
                    )
                    catalog_limit_reported = True
                continue
            selected[entry.name] = entry

    return SkillCatalog(
        entries=tuple(selected[name] for name in sorted(selected)),
        diagnostics=tuple(diagnostics),
    )


def _resolve_source_base(
    base: Path,
    specs: tuple[tuple[SkillSource, tuple[str, str]], ...],
) -> tuple[Path | None, tuple[SkillDiagnostic, ...]]:
    try:
        return base.expanduser().resolve(strict=False), ()
    except (OSError, RuntimeError) as exc:
        message = str(exc) or type(exc).__name__
        return (
            None,
            tuple(
                _diagnostic(
                    "root-unreadable",
                    "error",
                    f"cannot resolve skill source base: {message}",
                    source,
                    base.joinpath(*components),
                )
                for source, components in specs
            ),
        )


def _scan_root(root: _SkillRoot, *, context: ToolContext) -> _RootScan:
    path = root.path
    opened_root, open_diagnostic = _open_skill_root(root)
    if opened_root is None:
        if open_diagnostic is None:
            return _RootScan()
        return _RootScan(diagnostics=(open_diagnostic,))
    root_fd = opened_root.fd

    try:
        try:
            root_names = _bounded_root_names(root_fd, path=path)
        except _RootLimitError as exc:
            return _RootScan(
                diagnostics=(
                    _diagnostic(
                        "root-entry-limit",
                        "error",
                        str(exc),
                        root.source,
                        path,
                    ),
                )
            )
        except OSError as exc:
            return _RootScan(
                diagnostics=(
                    _diagnostic(
                        "root-unreadable",
                        "error",
                        f"cannot scan skill source root: {_os_error_message(exc)}",
                        root.source,
                        path,
                    ),
                )
            )

        # ``root.base`` is canonicalized before roots are constructed. Keep the
        # containment boundary lexical so a later path swap cannot redefine it.
        resolved_root = path
        entries: list[SkillEntry] = []
        diagnostics: list[SkillDiagnostic] = []
        frontmatter_bytes = 0
        for name, is_directory, is_symlink in root_names:
            candidate = path / name
            if is_symlink:
                diagnostics.append(
                    _diagnostic(
                        "entry-symlink",
                        "error",
                        "skill directory must not be a symlink",
                        root.source,
                        candidate,
                    )
                )
                continue
            if not is_directory:
                continue
            try:
                result, consumed, entry_diagnostics = _read_skill_entry(
                    root,
                    root_fd=root_fd,
                    resolved_root=resolved_root,
                    directory_name=name,
                    context=context,
                )
            except (OSError, RuntimeError) as exc:
                diagnostics.append(
                    _diagnostic(
                        "file-unreadable",
                        "error",
                        f"cannot inspect skill metadata: {exc}",
                        root.source,
                        candidate / "SKILL.md",
                    )
                )
                continue
            frontmatter_bytes += consumed
            if frontmatter_bytes > MAX_ROOT_FRONTMATTER_BYTES:
                diagnostics.append(
                    _diagnostic(
                        "root-metadata-limit",
                        "error",
                        (
                            "skill source root exceeds the "
                            f"{MAX_ROOT_FRONTMATTER_BYTES}-byte aggregate frontmatter limit"
                        ),
                        root.source,
                        path,
                    )
                )
                return _RootScan(diagnostics=tuple(diagnostics))
            diagnostics.extend(entry_diagnostics)
            if result is not None:
                entries.append(result)
        return _RootScan(entries=tuple(entries), diagnostics=tuple(diagnostics))
    finally:
        if root_fd is not None:
            os.close(root_fd)
        for guard in opened_root.guards:
            _close_windows_handle(guard)


def _open_skill_root(root: _SkillRoot) -> tuple[_OpenedRoot | None, SkillDiagnostic | None]:
    if not _USE_DESCRIPTOR_TRAVERSAL:
        if not _PATH_FALLBACK_SUPPORTED:
            return (
                None,
                _diagnostic(
                    "root-unreadable",
                    "error",
                    "secure skill source traversal is unavailable on this platform",
                    root.source,
                    root.path,
                ),
            )
        return _open_skill_root_by_path(root)

    try:
        current_fd = _open_canonical_base(root.base)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return (
            None,
            _diagnostic(
                "root-unreadable",
                "error",
                f"cannot open skill source base: {_os_error_message(exc)}",
                root.source,
                root.base,
            ),
        )

    current_path = root.base
    for component in root.components:
        candidate = current_path / component
        try:
            is_link = _is_link_like(candidate)
        except OSError as exc:
            os.close(current_fd)
            return (
                None,
                _diagnostic(
                    "root-unreadable",
                    "error",
                    f"cannot inspect skill source root: {_os_error_message(exc)}",
                    root.source,
                    candidate,
                ),
            )
        if is_link:
            os.close(current_fd)
            return (
                None,
                _diagnostic(
                    "root-symlink",
                    "error",
                    "skill source root components must not be symlinks",
                    root.source,
                    candidate,
                ),
            )
        try:
            next_fd = _open_relative(
                component,
                _DIRECTORY_FLAGS,
                directory_fd=current_fd,
                directory_path=current_path,
            )
        except FileNotFoundError:
            os.close(current_fd)
            return None, None
        except OSError as exc:
            os.close(current_fd)
            code: SkillDiagnosticCode = (
                "root-symlink"
                if exc.errno == errno.ELOOP
                else "root-not-directory"
                if exc.errno == errno.ENOTDIR
                else "root-unreadable"
            )
            return (
                None,
                _diagnostic(
                    code,
                    "error",
                    f"cannot open skill source root: {_os_error_message(exc)}",
                    root.source,
                    candidate,
                ),
            )
        os.close(current_fd)
        current_fd = next_fd
        current_path = candidate
    return _OpenedRoot(current_fd), None


def _open_canonical_base(base: Path) -> int:
    current_path = Path(base.anchor)
    current_fd = os.open(current_path, _DIRECTORY_FLAGS)
    try:
        for component in base.parts[1:]:
            next_fd = _open_relative(
                component,
                _DIRECTORY_FLAGS,
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


def _open_skill_root_by_path(
    root: _SkillRoot,
) -> tuple[_OpenedRoot | None, SkillDiagnostic | None]:
    if os.name == "nt":
        return _open_windows_skill_root(root)

    current_path = root.base
    try:
        base_stat = current_path.stat()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return (
            None,
            _diagnostic(
                "root-unreadable",
                "error",
                f"cannot inspect skill source base: {_os_error_message(exc)}",
                root.source,
                current_path,
            ),
        )
    if not stat.S_ISDIR(base_stat.st_mode):
        return (
            None,
            _diagnostic(
                "root-not-directory",
                "error",
                "skill source base is not a directory",
                root.source,
                current_path,
            ),
        )

    for component in root.components:
        candidate = current_path / component
        try:
            is_link = _is_link_like(candidate)
        except OSError as exc:
            return (
                None,
                _diagnostic(
                    "root-unreadable",
                    "error",
                    f"cannot inspect skill source root: {_os_error_message(exc)}",
                    root.source,
                    candidate,
                ),
            )
        if is_link:
            return (
                None,
                _diagnostic(
                    "root-symlink",
                    "error",
                    "skill source root components must not be symlinks or junctions",
                    root.source,
                    candidate,
                ),
            )
        try:
            candidate_stat = candidate.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None, None
        except OSError as exc:
            return (
                None,
                _diagnostic(
                    "root-unreadable",
                    "error",
                    f"cannot inspect skill source root: {_os_error_message(exc)}",
                    root.source,
                    candidate,
                ),
            )
        if not stat.S_ISDIR(candidate_stat.st_mode):
            return (
                None,
                _diagnostic(
                    "root-not-directory",
                    "error",
                    "skill source root component is not a directory",
                    root.source,
                    candidate,
                ),
            )
        current_path = candidate
    return _OpenedRoot(None), None


def _open_windows_skill_root(
    root: _SkillRoot,
) -> tuple[_OpenedRoot | None, SkillDiagnostic | None]:
    guards: list[int] = []
    for candidate in _windows_skill_root_paths(root):
        try:
            guard = _open_windows_directory_guard(candidate)
        except FileNotFoundError:
            for opened in guards:
                _close_windows_handle(opened)
            return None, None
        except OSError as exc:
            for opened in guards:
                _close_windows_handle(opened)
            return (
                None,
                _diagnostic(
                    "root-unreadable",
                    "error",
                    f"cannot open skill source root: {_os_error_message(exc)}",
                    root.source,
                    candidate,
                ),
            )
        guards.append(guard)

        try:
            is_link = _windows_handle_is_reparse_point(guard, path=candidate)
            resolved = _resolved_windows_handle(guard, path=candidate)
            candidate_stat = candidate.stat(follow_symlinks=False)
        except (OSError, RuntimeError) as exc:
            for opened in guards:
                _close_windows_handle(opened)
            return (
                None,
                _diagnostic(
                    "root-unreadable",
                    "error",
                    f"cannot inspect skill source root: {exc}",
                    root.source,
                    candidate,
                ),
            )
        if is_link or resolved != candidate:
            for opened in guards:
                _close_windows_handle(opened)
            return (
                None,
                _diagnostic(
                    "root-symlink",
                    "error",
                    "skill source root components must not be symlinks or junctions",
                    root.source,
                    candidate,
                ),
            )
        if not stat.S_ISDIR(candidate_stat.st_mode):
            for opened in guards:
                _close_windows_handle(opened)
            return (
                None,
                _diagnostic(
                    "root-not-directory",
                    "error",
                    "skill source root component is not a directory",
                    root.source,
                    candidate,
                ),
            )
    return _OpenedRoot(None, tuple(guards)), None


def _windows_skill_root_paths(root: _SkillRoot) -> tuple[Path, ...]:
    current = Path(root.base.anchor)
    paths = [current]
    for component in (*root.base.parts[1:], *root.components):
        current /= component
        paths.append(current)
    return tuple(paths)


def _bounded_root_names(root_fd: int | None, *, path: Path) -> tuple[tuple[str, bool, bool], ...]:
    entries: list[tuple[str, bool, bool]] = []
    scan_target: int | Path = root_fd if root_fd is not None else path
    with os.scandir(scan_target) as iterator:
        for item in iterator:
            entries.append(
                (
                    item.name,
                    item.is_dir(follow_symlinks=False),
                    item.is_symlink(),
                )
            )
            if len(entries) > MAX_ROOT_ENTRIES:
                raise _RootLimitError(
                    f"skill source root exceeds the {MAX_ROOT_ENTRIES}-entry limit"
                )
    return tuple(sorted(entries, key=lambda item: item[0]))


def _read_skill_entry(
    root: _SkillRoot,
    *,
    root_fd: int | None,
    resolved_root: Path,
    directory_name: str,
    context: ToolContext,
) -> tuple[SkillEntry | None, int, tuple[SkillDiagnostic, ...]]:
    skill_root = root.path / directory_name
    skill_file = skill_root / "SKILL.md"
    if _is_link_like(skill_root) or _is_link_like(skill_file):
        return (
            None,
            0,
            (
                _diagnostic(
                    "entry-symlink",
                    "error",
                    "skill directories and metadata files must not be symlinks or junctions",
                    root.source,
                    skill_file,
                ),
            ),
        )
    if is_protected_path(skill_file, context):
        return (
            None,
            0,
            (
                _diagnostic(
                    "protected-path",
                    "error",
                    "skill metadata path is protected",
                    root.source,
                    skill_file,
                ),
            ),
        )

    if root_fd is None:
        try:
            skill_guard = _open_path_skill_guard(skill_root)
        except FileNotFoundError:
            return None, 0, ()
        except OSError as exc:
            path_directory_code: SkillDiagnosticCode = (
                "entry-symlink" if exc.errno == errno.ELOOP else "file-unreadable"
            )
            return (
                None,
                0,
                (
                    _diagnostic(
                        path_directory_code,
                        "error",
                        f"cannot open skill directory: {_os_error_message(exc)}",
                        root.source,
                        skill_root,
                    ),
                ),
            )
        try:
            try:
                metadata_fd = _open_path_metadata(skill_file)
            except FileNotFoundError:
                return None, 0, ()
            except OSError as exc:
                return _metadata_open_error(root.source, skill_file, exc)
            try:
                resolved_file = _resolved_open_file(metadata_fd, path=skill_file)
                escape = _path_escape_diagnostic(
                    resolved_file,
                    resolved_root=resolved_root,
                    source=root.source,
                    skill_file=skill_file,
                )
                if escape is not None:
                    return None, 0, (escape,)
                if is_protected_path(resolved_file, context):
                    return (
                        None,
                        0,
                        (
                            _diagnostic(
                                "protected-path",
                                "error",
                                "opened skill metadata target is protected",
                                root.source,
                                skill_file,
                            ),
                        ),
                    )
                return _read_open_metadata(
                    metadata_fd,
                    source=root.source,
                    resolved_skill_root=resolved_file.parent,
                    directory_name=directory_name,
                    skill_file=skill_file,
                )
            finally:
                os.close(metadata_fd)
        finally:
            if skill_guard is not None:
                _close_windows_handle(skill_guard)

    try:
        skill_fd = _open_relative(
            directory_name,
            _DIRECTORY_FLAGS,
            directory_fd=root_fd,
            directory_path=root.path,
        )
    except OSError as exc:
        directory_code: SkillDiagnosticCode = (
            "entry-symlink" if exc.errno in {errno.ELOOP, errno.ENOTDIR} else "file-unreadable"
        )
        return (
            None,
            0,
            (
                _diagnostic(
                    directory_code,
                    "error",
                    f"cannot open skill directory: {_os_error_message(exc)}",
                    root.source,
                    skill_root,
                ),
            ),
        )

    try:
        identity_error = _skill_directory_identity_error(
            skill_fd,
            skill_root=skill_root,
            source=root.source,
            skill_file=skill_file,
        )
        if identity_error is not None:
            return None, 0, (identity_error,)
        try:
            metadata_fd = _open_relative(
                "SKILL.md",
                _FILE_FLAGS,
                directory_fd=skill_fd,
                directory_path=skill_root,
            )
        except FileNotFoundError:
            return None, 0, ()
        except OSError as exc:
            return _metadata_open_error(root.source, skill_file, exc)

        try:
            entry, consumed, diagnostics = _read_open_metadata(
                metadata_fd,
                source=root.source,
                resolved_skill_root=skill_root,
                directory_name=directory_name,
                skill_file=skill_file,
            )
            identity_error = _skill_directory_identity_error(
                skill_fd,
                skill_root=skill_root,
                source=root.source,
                skill_file=skill_file,
            )
            if identity_error is not None:
                return None, consumed, (*diagnostics, identity_error)
            return entry, consumed, diagnostics
        finally:
            os.close(metadata_fd)
    finally:
        os.close(skill_fd)


def _read_open_metadata(
    metadata_fd: int,
    *,
    source: SkillSource,
    resolved_skill_root: Path,
    directory_name: str,
    skill_file: Path,
) -> tuple[SkillEntry | None, int, tuple[SkillDiagnostic, ...]]:
    if not stat.S_ISREG(os.fstat(metadata_fd).st_mode):
        return (
            None,
            0,
            (
                _diagnostic(
                    "file-unreadable",
                    "error",
                    "SKILL.md is not a regular file",
                    source,
                    skill_file,
                ),
            ),
        )
    try:
        entry, consumed, diagnostics = read_skill_metadata(
            metadata_fd,
            source=source,
            skill_root=resolved_skill_root,
            directory_name=directory_name,
            skill_file=skill_file,
            max_frontmatter_bytes=MAX_FRONTMATTER_BYTES,
        )
    except SkillMetadataError as exc:
        return (
            None,
            exc.bytes_read,
            (
                _diagnostic(
                    exc.code,
                    "error",
                    str(exc),
                    source,
                    skill_file,
                ),
            ),
        )
    return entry, consumed, diagnostics


def _skill_directory_identity_error(
    skill_fd: int,
    *,
    skill_root: Path,
    source: SkillSource,
    skill_file: Path,
) -> SkillDiagnostic | None:
    opened = os.fstat(skill_fd)
    try:
        current = skill_root.stat(follow_symlinks=False)
    except OSError:
        current = None
    if current is not None and stat.S_ISDIR(current.st_mode):
        if (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino):
            return None
    return _diagnostic(
        "file-unreadable",
        "error",
        "skill directory changed while metadata was read",
        source,
        skill_file,
    )


def _metadata_open_error(
    source: SkillSource,
    skill_file: Path,
    exc: OSError,
) -> tuple[None, int, tuple[SkillDiagnostic, ...]]:
    metadata_code: SkillDiagnosticCode = (
        "entry-symlink" if exc.errno == errno.ELOOP else "file-unreadable"
    )
    return (
        None,
        0,
        (
            _diagnostic(
                metadata_code,
                "error",
                f"cannot open SKILL.md: {_os_error_message(exc)}",
                source,
                skill_file,
            ),
        ),
    )


def _path_escape_diagnostic(
    resolved_file: Path,
    *,
    resolved_root: Path,
    source: SkillSource,
    skill_file: Path,
) -> SkillDiagnostic | None:
    try:
        resolved_file.relative_to(resolved_root)
    except ValueError:
        return _diagnostic(
            "path-escape",
            "error",
            "skill metadata resolves outside its source root",
            source,
            skill_file,
        )
    return None


def _open_path_metadata(path: Path) -> int:
    return _shared_open_path_file(path)


def _open_path_skill_guard(path: Path) -> int | None:
    if os.name != "nt":
        return None
    return _open_windows_skill_directory_guard(path)


def _open_windows_skill_directory_guard(path: Path) -> int:
    handle = _open_windows_directory_guard(path)
    try:
        if _windows_handle_is_reparse_point(handle, path=path):
            raise OSError(errno.ELOOP, "skill directory is a reparse point", path)
        if _resolved_windows_handle(handle, path=path) != path.resolve(strict=False):
            raise OSError(errno.ELOOP, "skill directory changed while opening", path)
    except BaseException:
        _close_windows_handle(handle)
        raise
    return handle


def _open_windows_metadata(path: Path) -> int:
    handle = _open_windows_metadata_handle(path)
    try:
        if _windows_handle_is_reparse_point(handle, path=path):
            raise OSError(errno.ELOOP, "skill metadata is a reparse point", path)
        return _windows_handle_to_fd(handle)
    except BaseException:
        _close_windows_handle(handle)
        raise


def _windows_error(ctypes: Any, error: int, path: Path) -> OSError:
    return skill_filesystem.windows_error(ctypes, error, path)


def _diagnostic(
    code: SkillDiagnosticCode,
    severity: SkillDiagnosticSeverity,
    message: str,
    source: SkillSource,
    path: Path | None,
) -> SkillDiagnostic:
    return SkillDiagnostic(
        code=code,
        severity=severity,
        message=message,
        source=source,
        path=path,
    )


def _os_error_message(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__
