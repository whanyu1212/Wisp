"""Tests for bounded, trust-aware Agent Skills metadata discovery."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest
from pytest import MonkeyPatch

import wisp.skills.discovery as discovery_module
import wisp.skills.metadata as metadata_module
from wisp.skills import discover_skills


def _write_skill(
    source_root: Path,
    name: str,
    *,
    description: str = "Use when testing skills.",
    extra: str = "",
    body: bytes = b"# Instructions\nDo the work.\n",
) -> Path:
    skill_root = source_root / name
    skill_root.mkdir(parents=True)
    frontmatter = f"---\nname: {name}\ndescription: {description}\n{extra}---\n".encode()
    (skill_root / "SKILL.md").write_bytes(frontmatter + body)
    return skill_root


def _discover(home: Path, project: Path | None = None, *protected: str):
    return discover_skills(
        home_dir=home,
        project_root=project,
        protected_paths=protected,
    )


def test_discovers_valid_metadata_without_reading_the_body(tmp_path: Path) -> None:
    root = tmp_path / ".wisp" / "skills"
    skill = _write_skill(
        root,
        "pdf-tools",
        description="Handle PDFs when document processing is requested.",
        extra=(
            "license: Apache-2.0\n"
            "compatibility: Requires pdftotext\n"
            "metadata:\n  author: example\n  version: '1'\n"
            "allowed-tools: Bash(pdftotext:*) Read\n"
            "future-field: ignored\n"
        ),
        body=b"\xff\xfe body is deliberately not UTF-8",
    )

    catalog = _discover(tmp_path)

    assert catalog.names() == ("pdf-tools",)
    entry = catalog.get("pdf-tools")
    assert entry is not None
    assert entry.root == skill
    assert entry.source == "user:wisp"
    assert entry.license == "Apache-2.0"
    assert entry.compatibility == "Requires pdftotext"
    assert entry.metadata == (("author", "example"), ("version", "1"))
    assert entry.allowed_tools == "Bash(pdftotext:*) Read"
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["unsupported-field"]


def test_uses_yaml_12_scalar_rules_for_string_fields(tmp_path: Path) -> None:
    _write_skill(
        tmp_path / ".wisp" / "skills",
        "yaml-strings",
        description="1_2.3",
        extra=(
            "compatibility: 2026-08-08\n"
            "metadata:\n"
            "  legacy-bool: yes\n"
            "  on: off\n"
            "  leading-zero: 0123\n"
            "  underscore-int: 1_2\n"
        ),
    )

    catalog = _discover(tmp_path)

    entry = catalog.get("yaml-strings")
    assert entry is not None
    assert entry.description == "1_2.3"
    assert entry.compatibility == "2026-08-08"
    assert entry.metadata == (
        ("leading-zero", "0123"),
        ("legacy-bool", "yes"),
        ("on", "off"),
        ("underscore-int", "1_2"),
    )


def test_frontmatter_reader_does_not_read_ahead_into_skill_body(tmp_path: Path) -> None:
    skill = _write_skill(
        tmp_path / ".wisp" / "skills",
        "demo",
        body=b"x" * 16_384,
    )
    skill_file = skill / "SKILL.md"
    metadata_fd = os.open(skill_file, os.O_RDONLY)
    try:
        _, consumed, _ = metadata_module.read_skill_metadata(
            metadata_fd,
            source="user:wisp",
            skill_root=skill,
            directory_name="demo",
            skill_file=skill_file,
            max_frontmatter_bytes=16 * 1024,
        )

        assert os.lseek(metadata_fd, 0, os.SEEK_CUR) == consumed
    finally:
        os.close(metadata_fd)


@pytest.mark.parametrize(
    ("frontmatter", "code"),
    [
        (b"name: demo\ndescription: no delimiter\n", "invalid-frontmatter"),
        (b"---\nname: demo\ndescription: no close\n", "invalid-frontmatter"),
        (b"---\nname: demo\ndescription: \xff\n---\n", "invalid-frontmatter"),
        (b"---\nname: &name demo\ndescription: test\n---\n", "invalid-yaml"),
        (b"---\nname: demo\ndescription: *missing\n---\n", "invalid-yaml"),
        (b"---\nname: !!str demo\ndescription: test\n---\n", "invalid-yaml"),
        (b"---\nname: demo\nname: demo\ndescription: test\n---\n", "invalid-yaml"),
        (b"---\n- demo\n---\n", "invalid-metadata"),
    ],
)
def test_isolates_invalid_frontmatter(
    tmp_path: Path,
    frontmatter: bytes,
    code: str,
) -> None:
    skill_root = tmp_path / ".wisp" / "skills" / "demo"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_bytes(frontmatter)

    catalog = _discover(tmp_path)

    assert catalog.entries == ()
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == [code]


def test_isolates_yaml_recursion_failures_without_hiding_valid_skills(tmp_path: Path) -> None:
    root = tmp_path / ".wisp" / "skills"
    _write_skill(root, "valid")
    nested = "[" * 1_200 + "]" * 1_200
    _write_skill(root, "nested", extra=f"metadata:\n  nested: {nested}\n")

    catalog = _discover(tmp_path)

    assert catalog.names() == ("valid",)
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["invalid-yaml"]
    assert "nesting depth" in catalog.diagnostics[0].message


def test_isolates_yaml_constructor_value_errors(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    root = tmp_path / ".wisp" / "skills"
    _write_skill(root, "valid")
    _write_skill(root, "invalid-scalar")
    original_load = metadata_module.yaml.load

    def load_with_constructor_failure(stream: str, *args: object, **kwargs: object) -> object:
        if "name: invalid-scalar" in stream:
            raise ValueError("invalid scalar")
        return original_load(stream, *args, **kwargs)

    monkeypatch.setattr(metadata_module.yaml, "load", load_with_constructor_failure)

    catalog = _discover(tmp_path)

    assert catalog.names() == ("valid",)
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["invalid-yaml"]


@pytest.mark.parametrize(
    ("directory", "metadata", "message"),
    [
        ("demo", "description: missing name\n", "name is required"),
        ("demo", "name: demo\n", "description is required"),
        ("Demo", "name: Demo\ndescription: test\n", "name must be"),
        ("-demo", "name: -demo\ndescription: test\n", "name must be"),
        ("demo-", "name: demo-\ndescription: test\n", "name must be"),
        ("demo--skill", "name: demo--skill\ndescription: test\n", "name must be"),
        ("demo", 'name: " demo "\ndescription: test\n', "name must be"),
        ("other", "name: demo\ndescription: test\n", "does not match"),
        ("demo", "name: demo\ndescription: 42\n", "description is required"),
        ("demo", "name: demo\ndescription: 1.5\n", "description is required"),
        ("demo", "name: demo\ndescription: test\nmetadata: []\n", "metadata must"),
        (
            "demo",
            "name: demo\ndescription: test\nmetadata:\n  count: 2\n",
            "metadata must map",
        ),
    ],
)
def test_rejects_invalid_metadata(
    tmp_path: Path,
    directory: str,
    metadata: str,
    message: str,
) -> None:
    skill_root = tmp_path / ".wisp" / "skills" / directory
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(f"---\n{metadata}---\n", encoding="utf-8")

    catalog = _discover(tmp_path)

    assert catalog.entries == ()
    assert len(catalog.diagnostics) == 1
    assert catalog.diagnostics[0].code == "invalid-metadata"
    assert message in catalog.diagnostics[0].message


@pytest.mark.parametrize("name", ["on", "off", "yes", "no", "true", "2026-08-08", "0123"])
def test_preserves_plain_scalar_skill_names(tmp_path: Path, name: str) -> None:
    _write_skill(tmp_path / ".wisp" / "skills", name)

    catalog = _discover(tmp_path)

    assert catalog.names() == (name,)


def test_enforces_string_length_limits(tmp_path: Path) -> None:
    root = tmp_path / ".wisp" / "skills"
    _write_skill(root, "long-name", description="x" * 1025)
    _write_skill(
        root,
        "long-compatibility",
        extra=f"compatibility: {'x' * 501}\n",
    )

    catalog = _discover(tmp_path)

    assert catalog.entries == ()
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == [
        "invalid-metadata",
        "invalid-metadata",
    ]


def test_enforces_name_length_limit(tmp_path: Path) -> None:
    root = tmp_path / ".wisp" / "skills"
    valid_name = "a" * 64
    invalid_name = "b" * 65
    _write_skill(root, valid_name)
    _write_skill(root, invalid_name)

    catalog = _discover(tmp_path)

    assert catalog.names() == (valid_name,)
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["invalid-metadata"]


def test_enforces_frontmatter_byte_limit(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    root = tmp_path / ".wisp" / "skills"
    _write_skill(root, "demo", description="x" * 100)
    monkeypatch.setattr(discovery_module, "MAX_FRONTMATTER_BYTES", 64)

    catalog = _discover(tmp_path)

    assert catalog.entries == ()
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["invalid-frontmatter"]


def test_applies_documented_precedence_and_reports_shadowing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    roots = (
        (home / ".agents" / "skills", "user agents"),
        (home / ".wisp" / "skills", "user wisp"),
        (project / ".agents" / "skills", "project agents"),
        (project / ".wisp" / "skills", "project wisp"),
    )
    for root, description in roots:
        _write_skill(root, "review", description=description)

    catalog = _discover(home, project)

    assert catalog.names() == ("review",)
    assert catalog.entries[0].description == "project wisp"
    assert catalog.entries[0].source == "project:wisp"
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == [
        "shadowed",
        "shadowed",
        "shadowed",
    ]


def test_deduplicates_project_root_that_matches_home(tmp_path: Path) -> None:
    _write_skill(tmp_path / ".wisp" / "skills", "home-skill")

    catalog = _discover(tmp_path, tmp_path)

    assert catalog.names() == ("home-skill",)
    assert catalog.entries[0].source == "user:wisp"
    assert catalog.diagnostics == ()


def test_catalog_entries_are_sorted_by_name(tmp_path: Path) -> None:
    root = tmp_path / ".wisp" / "skills"
    _write_skill(root, "zebra")
    _write_skill(root, "alpha")
    _write_skill(root, "middle")

    assert _discover(tmp_path).names() == ("alpha", "middle", "zebra")


def test_project_roots_are_not_scanned_without_trust(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    scanned_sources: list[str] = []
    original = discovery_module._scan_root

    def recording_scan(root: object, **kwargs: object):
        scanned_sources.append(root.source)  # type: ignore[attr-defined]
        return original(root, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(discovery_module, "_scan_root", recording_scan)

    _discover(tmp_path / "home", project=None)

    assert scanned_sources == ["user:wisp", "user:agents"]


def test_home_resolution_failure_does_not_hide_valid_project_skills(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    _write_skill(project / ".wisp" / "skills", "valid")
    home.symlink_to(home, target_is_directory=True)

    catalog = _discover(home, project)

    assert catalog.names() == ("valid",)
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == [
        "root-unreadable",
        "root-unreadable",
    ]
    assert [diagnostic.source for diagnostic in catalog.diagnostics] == [
        "user:wisp",
        "user:agents",
    ]


def test_rejects_symlinked_roots_directories_and_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    external = tmp_path / "external"
    _write_skill(external, "root-skill")
    (home / ".wisp").mkdir(parents=True)
    (home / ".wisp" / "skills").symlink_to(external, target_is_directory=True)

    agents_root = home / ".agents" / "skills"
    agents_root.mkdir(parents=True)
    (agents_root / "linked-dir").symlink_to(external / "root-skill", target_is_directory=True)
    linked_file = agents_root / "linked-file"
    linked_file.mkdir()
    (linked_file / "SKILL.md").symlink_to(external / "root-skill" / "SKILL.md")

    catalog = _discover(home)

    assert catalog.entries == ()
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == [
        "root-symlink",
        "entry-symlink",
        "entry-symlink",
    ]


def test_rejects_intermediate_project_skill_root_symlink(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside-wisp"
    _write_skill(outside / "skills", "escaped")
    (project / ".wisp").symlink_to(outside, target_is_directory=True)

    catalog = _discover(home, project)

    assert catalog.entries == ()
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["root-symlink"]
    assert catalog.diagnostics[0].path == project / ".wisp"


def test_descriptor_traversal_rejects_swapped_source_base_ancestor(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    ancestor = tmp_path / "mutable"
    home = ancestor / "home"
    (home / ".wisp" / "skills" / "escaped").mkdir(parents=True)
    external_ancestor = tmp_path / "external"
    _write_skill(external_ancestor / "home" / ".wisp" / "skills", "escaped")
    moved_ancestor = tmp_path / "mutable-original"
    original_open = discovery_module.os.open
    raced = False

    def swap_ancestor_before_open(
        path: str | Path,
        flags: int,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal raced
        opening_directly = dir_fd is None and Path(path) == home
        opening_descriptor_relative = dir_fd is not None and path == ancestor.name
        if not raced and (opening_directly or opening_descriptor_relative):
            raced = True
            ancestor.rename(moved_ancestor)
            ancestor.symlink_to(external_ancestor, target_is_directory=True)
            try:
                return original_open(path, flags, dir_fd=dir_fd)
            finally:
                ancestor.unlink()
                moved_ancestor.rename(ancestor)
        return original_open(path, flags, dir_fd=dir_fd)

    monkeypatch.setattr(discovery_module.os, "open", swap_ancestor_before_open)

    catalog = _discover(home)

    assert raced is True
    assert catalog.entries == ()
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["root-unreadable"]


def test_descriptor_traversal_discards_entry_when_opened_skill_directory_is_replaced(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source_root = tmp_path / ".wisp" / "skills"
    skill_root = _write_skill(source_root, "swapped", description="original")
    moved_root = source_root / "swapped-original"
    original_read = discovery_module._read_open_metadata

    def replace_after_read(*args: object, **kwargs: object):
        result = original_read(*args, **kwargs)  # type: ignore[arg-type]
        skill_root.rename(moved_root)
        _write_skill(source_root, "swapped", description="replacement")
        return result

    monkeypatch.setattr(discovery_module, "_read_open_metadata", replace_after_read)

    catalog = _discover(tmp_path)

    assert catalog.entries == ()
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["file-unreadable"]


def test_path_fallback_discovers_skills_without_opening_directories(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _write_skill(tmp_path / ".wisp" / "skills", "windows-compatible")
    monkeypatch.setattr(discovery_module, "_USE_DESCRIPTOR_TRAVERSAL", False)
    monkeypatch.setattr(discovery_module, "_PATH_FALLBACK_SUPPORTED", True)
    monkeypatch.setattr(
        discovery_module,
        "_resolved_open_file",
        lambda metadata_fd, *, path: path.resolve(strict=False),
    )

    catalog = _discover(tmp_path)

    assert catalog.names() == ("windows-compatible",)


def test_windows_fallback_holds_component_guards_while_enumerating(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    root_path = tmp_path / ".wisp" / "skills"
    root_path.mkdir(parents=True)
    root = discovery_module._SkillRoot("user:wisp", tmp_path, (".wisp", "skills"))
    opened_paths: dict[int, Path] = {}
    closed: list[int] = []

    def open_guard(path: Path) -> int:
        handle = len(opened_paths) + 1
        opened_paths[handle] = path
        return handle

    def resolved_guard(handle: int, *, path: Path) -> Path:
        assert opened_paths[handle] == path
        return path.resolve(strict=False)

    def enumerate_while_guarded(root_fd: int | None, *, path: Path):
        assert root_fd is None
        assert path == root_path
        assert closed == []
        return ()

    monkeypatch.setattr(discovery_module, "_open_windows_directory_guard", open_guard)
    monkeypatch.setattr(discovery_module, "_resolved_windows_handle", resolved_guard)
    monkeypatch.setattr(discovery_module, "_close_windows_handle", closed.append)
    monkeypatch.setattr(discovery_module, "_bounded_root_names", enumerate_while_guarded)
    monkeypatch.setattr(
        discovery_module,
        "_open_skill_root",
        discovery_module._open_windows_skill_root,
    )

    scan = discovery_module._scan_root(
        root,
        context=discovery_module.ToolContext(cwd=tmp_path, protected_paths=()),
    )

    assert scan.entries == ()
    assert scan.diagnostics == ()
    assert closed == [1, 2, 3]


def test_rejects_path_fallback_without_stable_handle_validation(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _write_skill(tmp_path / ".wisp" / "skills", "unsupported")
    monkeypatch.setattr(discovery_module, "_USE_DESCRIPTOR_TRAVERSAL", False)
    monkeypatch.setattr(discovery_module, "_PATH_FALLBACK_SUPPORTED", False)

    catalog = _discover(tmp_path)

    assert catalog.entries == ()
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == [
        "root-unreadable",
        "root-unreadable",
    ]


def test_path_fallback_rejects_intermediate_root_symlink(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside-wisp"
    _write_skill(outside / "skills", "escaped")
    (home / ".wisp").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(discovery_module, "_USE_DESCRIPTOR_TRAVERSAL", False)
    monkeypatch.setattr(discovery_module, "_PATH_FALLBACK_SUPPORTED", True)

    catalog = _discover(home)

    assert catalog.entries == ()
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["root-symlink"]


def test_path_fallback_rejects_windows_junctions(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    _write_skill(home / ".wisp" / "skills", "escaped")
    monkeypatch.setattr(discovery_module, "_USE_DESCRIPTOR_TRAVERSAL", False)
    monkeypatch.setattr(discovery_module, "_PATH_FALLBACK_SUPPORTED", True)
    monkeypatch.setattr(
        discovery_module,
        "_is_link_like",
        lambda path: path == home / ".wisp",
    )

    catalog = _discover(home)

    assert catalog.entries == ()
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["root-symlink"]


def test_path_fallback_validates_open_file_handle_against_source_root(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    skill = _write_skill(tmp_path / ".wisp" / "skills", "swapped")
    outside = _write_skill(tmp_path / "outside", "swapped")
    monkeypatch.setattr(discovery_module, "_USE_DESCRIPTOR_TRAVERSAL", False)
    monkeypatch.setattr(discovery_module, "_PATH_FALLBACK_SUPPORTED", True)
    monkeypatch.setattr(
        discovery_module,
        "_resolved_open_file",
        lambda metadata_fd, *, path: outside / "SKILL.md",
    )

    catalog = _discover(tmp_path)

    assert catalog.entries == ()
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["path-escape"]
    assert catalog.diagnostics[0].path == skill / "SKILL.md"


def test_path_fallback_rechecks_protection_for_open_file_handle(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _write_skill(tmp_path / ".wisp" / "skills", "swapped")
    protected = tmp_path / ".wisp" / "skills" / "protected.md"
    protected.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(discovery_module, "_USE_DESCRIPTOR_TRAVERSAL", False)
    monkeypatch.setattr(discovery_module, "_PATH_FALLBACK_SUPPORTED", True)
    monkeypatch.setattr(
        discovery_module,
        "_resolved_open_file",
        lambda metadata_fd, *, path: protected,
    )

    catalog = _discover(tmp_path, None, "protected.md")

    assert catalog.entries == ()
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["protected-path"]


def test_path_fallback_rejects_metadata_reparse_swap(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _write_skill(tmp_path / ".wisp" / "skills", "swapped")
    monkeypatch.setattr(discovery_module, "_USE_DESCRIPTOR_TRAVERSAL", False)
    monkeypatch.setattr(discovery_module, "_PATH_FALLBACK_SUPPORTED", True)

    def reject_reparse_point(path: Path) -> int:
        raise OSError(errno.ELOOP, "skill metadata is a reparse point", path)

    monkeypatch.setattr(discovery_module, "_open_path_metadata", reject_reparse_point)

    catalog = _discover(tmp_path)

    assert catalog.entries == ()
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["entry-symlink"]


def test_windows_metadata_open_closes_reparse_handle(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    skill_file = tmp_path / "SKILL.md"
    closed: list[int] = []
    monkeypatch.setattr(discovery_module, "_open_windows_metadata_handle", lambda path: 42)
    monkeypatch.setattr(
        discovery_module,
        "_windows_handle_is_reparse_point",
        lambda handle, *, path: True,
    )
    monkeypatch.setattr(discovery_module, "_close_windows_handle", closed.append)

    with pytest.raises(OSError) as exc_info:
        discovery_module._open_windows_metadata(skill_file)

    assert exc_info.value.errno == errno.ELOOP
    assert closed == [42]


def test_windows_error_preserves_translated_subclass_and_path(tmp_path: Path) -> None:
    path = tmp_path / "missing"

    class FakeCtypes:
        @staticmethod
        def WinError(error: int) -> OSError:
            assert error == 3
            return FileNotFoundError(errno.ENOENT, "path not found")

    exc = discovery_module._windows_error(FakeCtypes(), 3, path)

    assert isinstance(exc, FileNotFoundError)
    assert exc.filename == str(path)


def test_catalog_stores_canonical_skill_root_through_symlinked_home_parent(
    tmp_path: Path,
) -> None:
    actual_home = tmp_path / "actual-home"
    skill = _write_skill(actual_home / ".wisp" / "skills", "demo")
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(actual_home, target_is_directory=True)

    catalog = _discover(linked_home)

    assert catalog.names() == ("demo",)
    assert catalog.entries[0].root == skill.resolve()


def test_rejects_protected_skill_metadata(tmp_path: Path) -> None:
    _write_skill(tmp_path / ".wisp" / "skills", "demo")

    catalog = _discover(tmp_path, None, "SKILL.md")

    assert catalog.entries == ()
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["protected-path"]


def test_non_directory_root_is_diagnostic(tmp_path: Path) -> None:
    root = tmp_path / ".wisp" / "skills"
    root.parent.mkdir(parents=True)
    root.write_text("not a directory", encoding="utf-8")

    catalog = _discover(tmp_path)

    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["root-not-directory"]


def test_unreadable_root_is_diagnostic(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    root = tmp_path / ".wisp" / "skills"
    root.mkdir(parents=True)
    original_open_relative = discovery_module._open_relative

    def deny_root(
        name: str,
        flags: int,
        *,
        directory_fd: int,
        directory_path: Path,
    ) -> int:
        if name == "skills" and directory_path == root.parent:
            raise PermissionError("denied")
        return original_open_relative(
            name,
            flags,
            directory_fd=directory_fd,
            directory_path=directory_path,
        )

    monkeypatch.setattr(discovery_module, "_open_relative", deny_root)

    catalog = _discover(tmp_path)

    assert catalog.entries == ()
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["root-unreadable"]


def test_link_inspection_error_closes_root_and_does_not_hide_later_sources(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _write_skill(tmp_path / ".wisp" / "skills", "blocked")
    _write_skill(tmp_path / ".agents" / "skills", "valid")
    original_open = discovery_module.os.open
    original_close = discovery_module.os.close
    original_is_link_like = discovery_module._is_link_like
    base_fds: list[int] = []
    closed_fds: list[int] = []

    def recording_open(path: str | Path, flags: int, *, dir_fd: int | None = None) -> int:
        fd = original_open(path, flags, dir_fd=dir_fd)
        if path == tmp_path.name and dir_fd is not None:
            base_fds.append(fd)
        return fd

    def recording_close(fd: int) -> None:
        closed_fds.append(fd)
        original_close(fd)

    def fail_wisp_inspection(path: Path) -> bool:
        if path == tmp_path / ".wisp":
            raise PermissionError("denied")
        return original_is_link_like(path)

    monkeypatch.setattr(discovery_module.os, "open", recording_open)
    monkeypatch.setattr(discovery_module.os, "close", recording_close)
    monkeypatch.setattr(discovery_module, "_is_link_like", fail_wisp_inspection)

    catalog = _discover(tmp_path)

    assert catalog.names() == ("valid",)
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["root-unreadable"]
    assert base_fds[0] in closed_fds


def test_scan_error_is_isolated_from_later_roots(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    wisp_root = tmp_path / ".wisp" / "skills"
    wisp_root.mkdir(parents=True)
    _write_skill(tmp_path / ".agents" / "skills", "valid")
    original = discovery_module._bounded_root_names

    def fail_wisp_root(root_fd: int | None, *, path: Path):
        if path == wisp_root:
            raise OSError("disconnected")
        return original(root_fd, path=path)

    monkeypatch.setattr(discovery_module, "_bounded_root_names", fail_wisp_root)

    catalog = _discover(tmp_path)

    assert catalog.names() == ("valid",)
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["root-unreadable"]


def test_unreadable_skill_file_does_not_hide_other_entries(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    root = tmp_path / ".wisp" / "skills"
    _write_skill(root, "blocked")
    _write_skill(root, "valid")
    original_open_relative = discovery_module._open_relative

    def deny_blocked(
        name: str,
        flags: int,
        *,
        directory_fd: int,
        directory_path: Path,
    ) -> int:
        if name == "SKILL.md" and directory_path.name == "blocked":
            raise PermissionError("denied")
        return original_open_relative(
            name,
            flags,
            directory_fd=directory_fd,
            directory_path=directory_path,
        )

    monkeypatch.setattr(discovery_module, "_open_relative", deny_blocked)

    catalog = _discover(tmp_path)

    assert catalog.names() == ("valid",)
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["file-unreadable"]


def test_metadata_files_are_opened_nonblocking(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    if not hasattr(discovery_module.os, "O_NONBLOCK"):
        pytest.skip("platform has no nonblocking file-open flag")
    _write_skill(tmp_path / ".wisp" / "skills", "demo")
    original_open_relative = discovery_module._open_relative

    def require_nonblocking(
        name: str,
        flags: int,
        *,
        directory_fd: int,
        directory_path: Path,
    ) -> int:
        if name == "SKILL.md":
            assert flags & discovery_module.os.O_NONBLOCK
        return original_open_relative(
            name,
            flags,
            directory_fd=directory_fd,
            directory_path=directory_path,
        )

    monkeypatch.setattr(discovery_module, "_open_relative", require_nonblocking)

    assert _discover(tmp_path).names() == ("demo",)


def test_non_regular_skill_file_is_diagnostic(tmp_path: Path) -> None:
    skill_file = tmp_path / ".wisp" / "skills" / "demo" / "SKILL.md"
    skill_file.mkdir(parents=True)

    catalog = _discover(tmp_path)

    assert catalog.entries == ()
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["file-unreadable"]


def test_rejects_entire_root_when_entry_limit_is_exceeded(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    root = tmp_path / ".wisp" / "skills"
    _write_skill(root, "one")
    _write_skill(root, "two")
    monkeypatch.setattr(discovery_module, "MAX_ROOT_ENTRIES", 1)

    catalog = _discover(tmp_path)

    assert catalog.entries == ()
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["root-entry-limit"]


def test_rejects_entire_root_when_aggregate_metadata_limit_is_exceeded(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    root = tmp_path / ".wisp" / "skills"
    _write_skill(root, "one")
    _write_skill(root, "two")
    monkeypatch.setattr(discovery_module, "MAX_ROOT_FRONTMATTER_BYTES", 80)

    catalog = _discover(tmp_path)

    assert catalog.entries == ()
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["root-metadata-limit"]


def test_catalog_limit_keeps_higher_precedence_entries(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    _write_skill(project / ".wisp" / "skills", "project-skill")
    _write_skill(home / ".wisp" / "skills", "user-skill")
    monkeypatch.setattr(discovery_module, "MAX_CATALOG_SKILLS", 1)

    catalog = _discover(home, project)

    assert catalog.names() == ("project-skill",)
    assert [diagnostic.code for diagnostic in catalog.diagnostics] == ["catalog-limit"]
