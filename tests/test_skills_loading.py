from __future__ import annotations

from pathlib import Path
from threading import get_ident
from typing import BinaryIO, cast

import anyio
import pytest

import wisp.skills.loading as loading_module
from wisp.skills.loading import load_skill_resource
from wisp.skills.models import SkillCatalog, SkillEntry
from wisp.skills.tool import SkillTool
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError


def _entry(tmp_path: Path) -> SkillEntry:
    root = tmp_path / "demo"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill\n---\nFollow these instructions.\n",
        encoding="utf-8",
    )
    return SkillEntry(name="demo", description="Demo skill", source="user:wisp", root=root)


def test_loads_instruction_body_without_frontmatter(tmp_path: Path) -> None:
    resource = load_skill_resource(
        _entry(tmp_path),
        None,
        context=ToolContext(cwd=tmp_path),
    )

    assert resource.text == "Follow these instructions.\n"
    assert resource.resource == "SKILL.md"
    assert resource.truncated is False


def test_loads_nested_relative_resource(tmp_path: Path) -> None:
    entry = _entry(tmp_path)
    references = entry.root / "references"
    references.mkdir()
    (references / "guide.md").write_text("supporting guide\n", encoding="utf-8")

    resource = load_skill_resource(
        entry,
        "references/guide.md",
        context=ToolContext(cwd=tmp_path),
    )

    assert resource.text == "supporting guide\n"
    assert resource.resource == "references/guide.md"


@pytest.mark.parametrize(
    "resource",
    [
        "",
        "/etc/passwd",
        "../secret",
        "refs/../secret",
        "refs/./secret",
        "a//b",
        "a\\b",
        ".env::$DATA",
        "SKILL.md::$DATA",
        "references/file.txt:stream",
    ],
)
def test_rejects_invalid_resource_paths(tmp_path: Path, resource: str) -> None:
    with pytest.raises(ToolError, match="skill|Skill"):
        load_skill_resource(
            _entry(tmp_path),
            resource,
            context=ToolContext(cwd=tmp_path),
        )


def test_rejects_symlinked_resource(tmp_path: Path) -> None:
    entry = _entry(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    (entry.root / "linked.md").symlink_to(outside)

    with pytest.raises(ToolError, match="link"):
        load_skill_resource(
            entry,
            "linked.md",
            context=ToolContext(cwd=tmp_path),
        )


def test_rejects_protected_resource_without_exposing_root(tmp_path: Path) -> None:
    entry = _entry(tmp_path)
    (entry.root / ".env").write_text("SECRET=value", encoding="utf-8")

    with pytest.raises(ToolError) as exc_info:
        load_skill_resource(entry, ".env", context=ToolContext(cwd=tmp_path))

    assert "protected" in str(exc_info.value)
    assert str(tmp_path) not in str(exc_info.value)


def test_bounds_resource_bytes_and_lines(tmp_path: Path) -> None:
    entry = _entry(tmp_path)
    (entry.root / "large.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    resource = load_skill_resource(
        entry,
        "large.txt",
        context=ToolContext(cwd=tmp_path, max_output_bytes=32, max_output_lines=2),
    )

    assert resource.text.endswith("[truncated]")
    assert "three" not in resource.text
    assert resource.truncated is True


def test_resource_reader_bounds_each_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "resource"
    path.write_bytes(b"unused")
    file_fd = loading_module.os.open(path, loading_module.FILE_FLAGS)
    read_sizes: list[int] = []

    class BoundedStream:
        def __init__(self, duplicate_fd: int) -> None:
            self.duplicate_fd = duplicate_fd

        def __enter__(self) -> BoundedStream:
            return self

        def __exit__(self, *args: object) -> None:
            loading_module.os.close(self.duplicate_fd)

        def readline(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            if size < 0:
                raise AssertionError("skill resource reads must be size-limited")
            return b"x" * size

        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            if size < 0:
                raise AssertionError("skill resource probes must be size-limited")
            return b"x" * size

    def bounded_fdopen(
        duplicate_fd: int,
        mode: str,
        buffering: int = -1,
    ) -> BinaryIO:
        assert mode == "rb"
        assert buffering == 0
        return cast(BinaryIO, BoundedStream(duplicate_fd))

    monkeypatch.setattr(loading_module.os, "fdopen", bounded_fdopen)
    try:
        text, truncated = loading_module._read_bounded_text(
            file_fd,
            max_bytes=8,
            max_lines=2,
            resource="resource",
        )
    finally:
        loading_module.os.close(file_fd)

    assert text == "x" * 8
    assert truncated is True
    assert read_sizes == [9]


def test_rejects_invalid_utf8(tmp_path: Path) -> None:
    entry = _entry(tmp_path)
    (entry.root / "binary.dat").write_bytes(b"valid\n\xffbad")

    with pytest.raises(ToolError, match="UTF-8"):
        load_skill_resource(
            entry,
            "binary.dat",
            context=ToolContext(cwd=tmp_path),
        )


def test_rejects_invalid_utf8_at_byte_boundary(tmp_path: Path) -> None:
    entry = _entry(tmp_path)
    (entry.root / "invalid.dat").write_bytes(b"valid\xffmore")

    with pytest.raises(ToolError, match="UTF-8"):
        load_skill_resource(
            entry,
            "invalid.dat",
            context=ToolContext(cwd=tmp_path, max_output_bytes=6),
        )


def test_tolerates_valid_utf8_split_by_byte_boundary(tmp_path: Path) -> None:
    path = tmp_path / "split.txt"
    path.write_bytes(b"valid\xe2\x82\xacmore")
    file_fd = loading_module.os.open(path, loading_module.FILE_FLAGS)
    try:
        text, truncated = loading_module._read_bounded_text(
            file_fd,
            max_bytes=6,
            max_lines=10,
            resource="split.txt",
        )
    finally:
        loading_module.os.close(file_fd)

    assert text == "valid"
    assert truncated is True


def test_skill_tool_runs_loading_off_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry(tmp_path)
    tool = SkillTool(SkillCatalog(entries=(entry,)))
    calls: list[str | None] = []
    caller_thread = get_ident()

    def recording_loader(
        selected: SkillEntry,
        resource: str | None,
        *,
        context: ToolContext,
    ):
        assert get_ident() != caller_thread
        calls.append(resource)
        return load_skill_resource(selected, resource, context=context)

    monkeypatch.setattr("wisp.skills.tool.load_skill_resource", recording_loader)

    async def scenario() -> None:
        result = await tool.run({"name": "demo"}, ToolContext(cwd=tmp_path))
        assert result.text == "Follow these instructions.\n"

    anyio.run(scenario)
    assert calls == [None]


def test_skill_tool_rejects_unknown_name_without_paths(tmp_path: Path) -> None:
    tool = SkillTool()

    async def scenario() -> None:
        with pytest.raises(ToolError) as exc_info:
            await tool.run({"name": "missing"}, ToolContext(cwd=tmp_path))
        assert "available skills: none" in str(exc_info.value)
        assert str(tmp_path) not in str(exc_info.value)

    anyio.run(scenario)
