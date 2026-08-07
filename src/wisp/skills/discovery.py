"""Strict, bounded discovery of Agent Skills metadata."""

from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass
from pathlib import Path

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

_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd
_SCANDIR_SUPPORTS_FD = os.scandir in os.supports_fd


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

    home = home_dir.expanduser().resolve(strict=False)
    roots: list[_SkillRoot] = []
    if project_root is not None:
        project = project_root.expanduser().resolve(strict=False)
        roots.extend(
            (
                _SkillRoot("project:wisp", project, (".wisp", "skills")),
                _SkillRoot("project:agents", project, (".agents", "skills")),
            )
        )
    roots.extend(
        (
            _SkillRoot("user:wisp", home, (".wisp", "skills")),
            _SkillRoot("user:agents", home, (".agents", "skills")),
        )
    )

    context = ToolContext(
        cwd=project_root or home,
        protected_paths=protected_paths,
    )
    selected: dict[str, SkillEntry] = {}
    diagnostics: list[SkillDiagnostic] = []
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


def _scan_root(root: _SkillRoot, *, context: ToolContext) -> _RootScan:
    path = root.path
    root_fd, open_diagnostic = _open_skill_root(root)
    if root_fd is None:
        if open_diagnostic is None:
            return _RootScan()
        return _RootScan(diagnostics=(open_diagnostic,))

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

        resolved_root = path.resolve(strict=False)
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
            result, consumed, entry_diagnostics = _read_skill_entry(
                root,
                root_fd=root_fd,
                resolved_root=resolved_root,
                directory_name=name,
                context=context,
            )
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
        os.close(root_fd)


def _open_skill_root(root: _SkillRoot) -> tuple[int | None, SkillDiagnostic | None]:
    try:
        current_fd = os.open(root.base, _DIRECTORY_FLAGS)
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
        if candidate.is_symlink():
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
    return current_fd, None


def _bounded_root_names(root_fd: int, *, path: Path) -> tuple[tuple[str, bool, bool], ...]:
    entries: list[tuple[str, bool, bool]] = []
    scan_target: int | Path = root_fd if _SCANDIR_SUPPORTS_FD else path
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
    root_fd: int,
    resolved_root: Path,
    directory_name: str,
    context: ToolContext,
) -> tuple[SkillEntry | None, int, tuple[SkillDiagnostic, ...]]:
    skill_root = root.path / directory_name
    skill_file = skill_root / "SKILL.md"
    if skill_file.is_symlink():
        return (
            None,
            0,
            (
                _diagnostic(
                    "entry-symlink",
                    "error",
                    "SKILL.md must not be a symlink",
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

    resolved_file = skill_file.resolve(strict=False)
    try:
        resolved_file.relative_to(resolved_root)
    except ValueError:
        return (
            None,
            0,
            (
                _diagnostic(
                    "path-escape",
                    "error",
                    "skill metadata resolves outside its source root",
                    root.source,
                    skill_file,
                ),
            ),
        )

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
                        root.source,
                        skill_file,
                    ),
                ),
            )

        try:
            if not stat.S_ISREG(os.fstat(metadata_fd).st_mode):
                return (
                    None,
                    0,
                    (
                        _diagnostic(
                            "file-unreadable",
                            "error",
                            "SKILL.md is not a regular file",
                            root.source,
                            skill_file,
                        ),
                    ),
                )
            try:
                entry, consumed, diagnostics = read_skill_metadata(
                    metadata_fd,
                    source=root.source,
                    skill_root=resolved_file.parent,
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
                            root.source,
                            skill_file,
                        ),
                    ),
                )
        finally:
            os.close(metadata_fd)
    finally:
        os.close(skill_fd)
    return entry, consumed, diagnostics


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


def _open_relative(
    name: str,
    flags: int,
    *,
    directory_fd: int,
    directory_path: Path,
) -> int:
    if _OPEN_SUPPORTS_DIR_FD:
        return os.open(name, flags, dir_fd=directory_fd)
    return os.open(directory_path / name, flags)
