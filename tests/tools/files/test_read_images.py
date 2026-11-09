"""The read tool returns supported image files as images rather than text."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.tools.support import run_tool
from wisp.images import MAX_IMAGE_BYTES, inspect_image
from wisp.tools.builtin import ReadTool
from wisp.tools.context import ToolContext
from wisp.tools.files.secure_fs import file_version
from wisp.tools.result import ToolError
from wisp.tools.seen_files import SeenFiles

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "images"


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("pixel.png", "PNG"),
        ("pixel.jpg", "JPEG"),
        ("pixel.gif", "GIF"),
        ("pixel-lossy.webp", "WebP"),
    ],
)
def test_image_file_returns_bytes(tmp_path: Path, name: str, label: str) -> None:
    data = (_FIXTURES / name).read_bytes()
    (tmp_path / name).write_bytes(data)
    seen = SeenFiles()

    result = run_tool(ReadTool(), {"path": name}, ToolContext(cwd=tmp_path, seen_files=seen))

    image = inspect_image(data)
    assert result.images == (data,)
    assert result.text == (
        f"Image file {name}: {label}, {image.width}x{image.height} px, {len(data)} bytes."
    )
    assert result.data == {"path": name}
    # Reading the image counts as seeing this version of the file.
    path = tmp_path / name
    seen.require_current(path, file_version(path.stat()), display=name)


def test_slice_arguments_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes((_FIXTURES / "pixel.png").read_bytes())

    result = run_tool(ReadTool(), {"path": "shot.png", "offset": 3}, ToolContext(cwd=tmp_path))

    assert len(result.images) == 1
    assert result.text.endswith("offset and limit apply only to text files and were ignored.")


@pytest.mark.parametrize(
    ("header", "label"),
    [
        (b"BM6\x00\x00\x00\x00\x00\x00\x006\x00\x00\x00", "BMP"),
        (b"II*\x00\x08\x00\x00\x00", "TIFF"),
        (b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00", "HEIC"),
    ],
)
def test_unsupported_format_is_named(tmp_path: Path, header: bytes, label: str) -> None:
    (tmp_path / "picture").write_bytes(header + b"\x00" * 64)

    with pytest.raises(ToolError, match=f"picture is a {label} image; read shows PNG"):
        run_tool(ReadTool(), {"path": "picture"}, ToolContext(cwd=tmp_path))


def test_oversized_image_is_refused(tmp_path: Path) -> None:
    png = (_FIXTURES / "pixel.png").read_bytes()
    (tmp_path / "huge.png").write_bytes(png + b"\x00" * MAX_IMAGE_BYTES)

    with pytest.raises(ToolError, match=f"over the {MAX_IMAGE_BYTES}-byte limit"):
        run_tool(ReadTool(), {"path": "huge.png"}, ToolContext(cwd=tmp_path))


def test_damaged_image_is_refused(tmp_path: Path) -> None:
    png = (_FIXTURES / "pixel.png").read_bytes()
    (tmp_path / "cut.png").write_bytes(png[: len(png) // 2])

    with pytest.raises(ToolError, match="Cannot show cut.png: Malformed PNG"):
        run_tool(ReadTool(), {"path": "cut.png"}, ToolContext(cwd=tmp_path))


@pytest.mark.parametrize("text", ["BMW service notes\n", "GIF89a release notes\n"])
def test_text_like_signatures_stay_text(tmp_path: Path, text: str) -> None:
    """Text that starts like an image signature ("BM", "GIF89a") is still read as text."""

    (tmp_path / "notes.txt").write_text(f"{text}second line\n")

    result = run_tool(ReadTool(), {"path": "notes.txt"}, ToolContext(cwd=tmp_path))

    assert result.images == ()
    assert result.text.startswith(f"{text}second line")


def test_protected_image_is_refused(tmp_path: Path) -> None:
    (tmp_path / "secret.png").write_bytes((_FIXTURES / "pixel.png").read_bytes())
    context = ToolContext(cwd=tmp_path, protected_paths=("secret.png",))

    with pytest.raises(ToolError, match="protected path"):
        run_tool(ReadTool(), {"path": "secret.png"}, context)


def test_image_outside_workspace_is_refused(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "outside.png").write_bytes((_FIXTURES / "pixel.png").read_bytes())
    (project / "link.png").symlink_to(tmp_path / "outside.png")

    with pytest.raises(ToolError) as refused:
        run_tool(ReadTool(), {"path": "link.png"}, ToolContext(cwd=project))

    assert refused.value.failure_code == "path_outside_workspace"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_fifo_named_like_image_is_refused(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe.png")

    with pytest.raises(ToolError):
        run_tool(ReadTool(), {"path": "pipe.png"}, ToolContext(cwd=tmp_path))
