from __future__ import annotations

import hashlib
import random
import struct
import zlib
from pathlib import Path

import pytest

from wisp.images import MAX_IMAGE_BYTES, MAX_IMAGE_SIDE, ImageRejectedError, inspect_image

_FIXTURES = Path(__file__).parent / "fixtures" / "images"


def _fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def _png_with_size(width: int, height: int, *, total_bytes: int | None = None) -> bytes:
    """A structurally valid PNG header that declares any dimensions.

    ``total_bytes`` pads the file between IHDR and IEND to an exact length.
    """

    ihdr = b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    head = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + ihdr + struct.pack(">I", zlib.crc32(ihdr))
    tail = b"\x00\x00\x00\x00IEND\xaeB`\x82"
    padding = b"" if total_bytes is None else b"\x00" * (total_bytes - len(head) - len(tail))
    return head + padding + tail


@pytest.mark.parametrize(
    ("name", "media_type"),
    [
        ("pixel.png", "image/png"),
        ("pixel.jpg", "image/jpeg"),
        ("pixel.gif", "image/gif"),
        ("pixel-lossy.webp", "image/webp"),
        ("pixel-lossless.webp", "image/webp"),
        ("pixel-extended.webp", "image/webp"),
    ],
)
def test_reads_format_and_size(name: str, media_type: str) -> None:
    data = _fixture(name)

    image = inspect_image(data)

    assert image.media_type == media_type
    assert (image.width, image.height) == (3, 2)
    assert image.byte_size == len(data)
    assert image.sha256 == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize(("width", "height"), [(MAX_IMAGE_SIDE, 1), (1, MAX_IMAGE_SIDE)])
def test_accepts_largest_allowed_side(width: int, height: int) -> None:
    image = inspect_image(_png_with_size(width, height))

    assert (image.width, image.height) == (width, height)


def test_accepts_largest_allowed_file() -> None:
    image = inspect_image(_png_with_size(1, 1, total_bytes=MAX_IMAGE_BYTES))

    assert image.byte_size == MAX_IMAGE_BYTES


def _corrupt_png_checksum() -> bytes:
    data = bytearray(_fixture("pixel.png"))
    data[29] ^= 0xFF
    return bytes(data)


def _webp_with_wrong_riff_size() -> bytes:
    data = bytearray(_fixture("pixel-lossy.webp"))
    data[4:8] = struct.pack("<I", len(data))
    return bytes(data)


def _webp(chunk_type: bytes, payload: bytes, *, chunk_size: int | None = None) -> bytes:
    size = len(payload) if chunk_size is None else chunk_size
    body = b"WEBP" + chunk_type + struct.pack("<I", size) + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _png_with_chunk_type(chunk_type: bytes) -> bytes:
    data = bytearray(_fixture("pixel.png"))
    data[12:16] = chunk_type
    return bytes(data)


def test_jpeg_skips_fill_and_standalone_markers() -> None:
    # SOI, TEM, two fill bytes, then a baseline frame header for a 3x2 image, EOI.
    frame = b"\xff\xc0\x00\x0b\x08\x00\x02\x00\x03\x01\x01\x11\x00"
    empty_app0 = b"\xff\xe0\x00\x02"
    image = inspect_image(b"\xff\xd8\xff\x01\xff\xff" + empty_app0 + frame + b"\xff\xd9")

    assert (image.width, image.height) == (3, 2)


def _jpeg_without_frame() -> bytes:
    # SOI, one APP0 segment, then start-of-scan and EOI with no frame header.
    return b"\xff\xd8\xff\xe0\x00\x04ab\xff\xda\x00\x02\xff\xd9"


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"", "empty"),
        (b"\x00" * (MAX_IMAGE_BYTES + 1), "too_large"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "unsupported_format"),
        (b"BM" + b"\x00" * 64, "unsupported_format"),
        (_fixture("pixel.png")[:-12], "malformed"),
        (_corrupt_png_checksum(), "malformed"),
        (_fixture("pixel.png")[:20], "malformed"),
        (_fixture("pixel.jpg")[:-2], "malformed"),
        (_jpeg_without_frame(), "malformed"),
        (b"\xff\xd8\xff\xc0\x7f\xff\x08\xff\xd9", "malformed"),
        (_fixture("pixel.gif")[:-1], "malformed"),
        (_fixture("pixel-lossy.webp")[:-4], "malformed"),
        (_webp_with_wrong_riff_size(), "malformed"),
        (_png_with_chunk_type(b"IDAT"), "malformed"),
        (b"GIF89a\x01\x00;", "malformed"),
        (b"\xff\xd8\x00\xff\xd9", "malformed"),
        (b"\xff\xd8\xff\xc0\x00\x05\x08\x00\x02\xff\xd9", "malformed"),
        (b"\xff\xd8\xff\xe0\x00\x04\xff\xd9", "malformed"),
        (b"RIFF\x04\x00\x00\x00WEBP", "malformed"),
        (_webp(b"VP8 ", b"\x00" * 4, chunk_size=64), "malformed"),
        (_webp(b"VP8 ", b"\x00" * 6 + struct.pack("<HH", 3, 2)), "malformed"),
        (_webp(b"VP8L", b"\x00" * 5), "malformed"),
        (_webp(b"VP8X", b"\x00" * 6), "malformed"),
        (_webp(b"ALPH", b"\x00" * 4), "malformed"),
        (b"RIFF\x04\x00\x00\x00AVI ", "unsupported_format"),
        (_png_with_size(0, 5), "malformed"),
        (_png_with_size(5, 0), "malformed"),
        (_png_with_size(MAX_IMAGE_SIDE + 1, 1), "dimensions_too_large"),
        (_png_with_size(1, 2**31), "dimensions_too_large"),
    ],
    ids=[
        "empty",
        "over-byte-limit",
        "svg",
        "bmp",
        "png-without-iend",
        "png-bad-checksum",
        "png-cut-in-header",
        "jpeg-without-eoi",
        "jpeg-without-frame",
        "jpeg-segment-overrun",
        "gif-without-trailer",
        "webp-truncated",
        "webp-wrong-riff-size",
        "png-first-chunk-not-ihdr",
        "gif-cut-in-header",
        "jpeg-missing-marker",
        "jpeg-short-frame",
        "jpeg-segment-swallows-eoi",
        "webp-cut-in-header",
        "webp-chunk-overrun",
        "webp-lossy-bad-start-code",
        "webp-lossless-bad-signature",
        "webp-extended-too-short",
        "webp-unknown-chunk",
        "riff-but-not-webp",
        "zero-width",
        "zero-height",
        "too-wide",
        "huge-declared-height",
    ],
)
def test_rejects_invalid_images(data: bytes, reason: str) -> None:
    with pytest.raises(ImageRejectedError) as raised:
        inspect_image(data)

    assert raised.value.reason == reason


@pytest.mark.parametrize(
    "name",
    [
        "pixel.png",
        "pixel.jpg",
        "pixel.gif",
        "pixel-lossy.webp",
        "pixel-lossless.webp",
        "pixel-extended.webp",
    ],
)
def test_damaged_files_never_crash(name: str) -> None:
    """Every truncation and seeded byte flip either parses or is rejected cleanly."""

    data = _fixture(name)
    rng = random.Random(name)
    damaged = [data[:length] for length in range(len(data))]
    for _ in range(300):
        flipped = bytearray(data)
        flipped[rng.randrange(len(data))] = rng.randrange(256)
        damaged.append(bytes(flipped))

    for candidate in damaged:
        try:
            image = inspect_image(candidate)
        except ImageRejectedError:
            continue
        assert 0 < image.width <= MAX_IMAGE_SIDE
        assert 0 < image.height <= MAX_IMAGE_SIDE
