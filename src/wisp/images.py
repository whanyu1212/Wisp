"""Validate image bytes before Wisp stores them or sends them to a model.

Images are inspected, never decoded: the format comes from the file's magic
bytes and the dimensions from its header, and a structural end-of-file check
catches truncated files. Capping the declared dimensions guards against
decompression bombs without needing an image library.
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from collections.abc import Callable
from typing import Literal

from wisp.events import ImageMediaType, ImagePart

# Claude accepts at most 10 MB of base64 per image, the tightest per-image limit
# among the supported vision APIs; base64 grows bytes by 4/3.
MAX_IMAGE_BYTES = 7_500_000
# Claude rejects images larger than 8000x8000 px.
MAX_IMAGE_SIDE = 8000

type ImageRejectionReason = Literal[
    "empty",
    "too_large",
    "unsupported_format",
    "malformed",
    "dimensions_too_large",
]

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_IEND_CHUNK = b"\x00\x00\x00\x00IEND\xaeB`\x82"
_GIF_SIGNATURES = (b"GIF87a", b"GIF89a")
_GIF_TRAILER = 0x3B
_JPEG_START = b"\xff\xd8"
_JPEG_END = b"\xff\xd9"
# Start-of-frame markers carry the dimensions; C4 (DHT), C8 (JPG) and CC (DAC)
# share the range but are not frames.
_JPEG_FRAME_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
# Markers with no length field: TEM and the restart markers.
_JPEG_STANDALONE_MARKERS = frozenset({0x01, *range(0xD0, 0xD8)})
_JPEG_START_OF_SCAN = 0xDA


class ImageRejectedError(ValueError):
    """Image bytes that Wisp will not store or send.

    Attributes:
        reason (ImageRejectionReason): Which check failed, for typed error reports.
    """

    def __init__(self, reason: ImageRejectionReason, message: str) -> None:
        super().__init__(message)
        self.reason: ImageRejectionReason = reason


def inspect_image(data: bytes) -> ImagePart:
    """Identify an image from its content and check it against Wisp's limits.

    Args:
        data (bytes): The complete image file.

    Returns:
        ImagePart: The format, dimensions, size and SHA-256 digest of ``data``.

    Raises:
        ImageRejectedError: The bytes are empty, larger than ``MAX_IMAGE_BYTES``,
            not PNG, JPEG, GIF or WebP, structurally invalid or truncated, or
            declare a side longer than ``MAX_IMAGE_SIDE``.
    """

    if not data:
        raise ImageRejectedError("empty", "Image is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageRejectedError(
            "too_large",
            f"Image is {len(data)} bytes; the limit is {MAX_IMAGE_BYTES} bytes",
        )
    media_type, width, height = _identify(data)
    if width <= 0 or height <= 0:
        raise ImageRejectedError("malformed", f"Image declares invalid dimensions {width}x{height}")
    if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
        raise ImageRejectedError(
            "dimensions_too_large",
            f"Image is {width}x{height} px; each side must be at most {MAX_IMAGE_SIDE} px",
        )
    return ImagePart(
        media_type=media_type,
        sha256=hashlib.sha256(data).hexdigest(),
        byte_size=len(data),
        width=width,
        height=height,
    )


# Enough leading bytes to recognise every format `image_format` names.
IMAGE_SNIFF_BYTES = 16
# Common image formats that models do not accept, recognised so a caller can say
# why such a file is refused instead of treating it as text.
_UNSUPPORTED_FORMATS: tuple[tuple[str, Callable[[bytes], bool]], ...] = (
    # "BM", the file size, then four reserved bytes that are always zero; the zero
    # bytes keep a text file that happens to start with "BM" from matching.
    ("BMP", lambda header: header.startswith(b"BM") and header[6:10] == b"\x00" * 4),
    ("TIFF", lambda header: header[:4] in (b"II*\x00", b"MM\x00*")),
    ("HEIC", lambda header: header[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1")),
    ("AVIF", lambda header: header[4:12] == b"ftypavif"),
)


def image_format(header: bytes) -> tuple[str, ImageMediaType | None] | None:
    """Name the image format a file's first bytes announce.

    Only the signature is checked; ``inspect_image`` still validates the whole file.

    Args:
        header (bytes): At least the first ``IMAGE_SNIFF_BYTES`` of the file, or the
            whole file when it is shorter.

    Returns:
        tuple[str, ImageMediaType | None] | None: The format's name and, when Wisp
        can send it to a model, its media type; None for anything that is not a
        recognised image.

    Examples:
        >>> image_format(b"\\x89PNG\\r\\n\\x1a\\n")
        ('PNG', 'image/png')
        >>> image_format(b"BM6\\x00\\x00\\x00\\x00\\x00\\x00\\x00")
        ('BMP', None)
        >>> image_format(b"plain text") is None
        True
        >>> image_format(b"GIF89a release notes") is None
        True
    """

    if header.startswith(_PNG_SIGNATURE):
        return "PNG", "image/png"
    if header.startswith(_JPEG_START + b"\xff"):
        return "JPEG", "image/jpeg"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "WebP", "image/webp"
    # The remaining signatures are plain ASCII, so a text file could start with one;
    # every real such image has a control byte in its header (a GIF's dimensions
    # stay below 0x2000, a HEIC or AVIF box size starts with zero bytes).
    if _looks_like_text(header):
        return None
    if header[:6] in _GIF_SIGNATURES:
        return "GIF", "image/gif"
    for name, matches in _UNSUPPORTED_FORMATS:
        if matches(header):
            return name, None
    return None


def _looks_like_text(header: bytes) -> bool:
    """Whether a header has no byte that text files avoid (controls other than
    tab, newline and carriage return, or DEL); UTF-8 sequences count as text."""

    return all(byte >= 0x20 and byte != 0x7F or byte in b"\t\n\r" for byte in header)


def _identify(data: bytes) -> tuple[ImageMediaType, int, int]:
    if data.startswith(_PNG_SIGNATURE):
        return ("image/png", *_png_dimensions(data))
    if data.startswith(_JPEG_START):
        return ("image/jpeg", *_jpeg_dimensions(data))
    if data[:6] in _GIF_SIGNATURES:
        return ("image/gif", *_gif_dimensions(data))
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ("image/webp", *_webp_dimensions(data))
    raise ImageRejectedError("unsupported_format", "Image is not a PNG, JPEG, GIF or WebP file")


def _malformed(media_type: str, detail: str) -> ImageRejectedError:
    return ImageRejectedError("malformed", f"Malformed {media_type} image: {detail}")


def _png_dimensions(data: bytes) -> tuple[int, int]:
    # The first chunk must be a 13-byte IHDR: signature(8) length(4) type(4) data(13) crc(4).
    if len(data) < 33 + len(_PNG_IEND_CHUNK):
        raise _malformed("PNG", "file is too short")
    length, chunk_type = struct.unpack(">I4s", data[8:16])
    if length != 13 or chunk_type != b"IHDR":
        raise _malformed("PNG", "first chunk is not IHDR")
    (crc,) = struct.unpack(">I", data[29:33])
    if zlib.crc32(data[12:29]) != crc:
        raise _malformed("PNG", "IHDR checksum does not match")
    if not data.endswith(_PNG_IEND_CHUNK):
        raise _malformed("PNG", "file does not end with IEND")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _gif_dimensions(data: bytes) -> tuple[int, int]:
    # Header(6), logical screen width and height (little-endian u16), then flags.
    if len(data) < 14:
        raise _malformed("GIF", "file is too short")
    if data[-1] != _GIF_TRAILER:
        raise _malformed("GIF", "file does not end with the GIF trailer")
    width, height = struct.unpack("<HH", data[6:10])
    return width, height


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if not data.endswith(_JPEG_END):
        raise _malformed("JPEG", "file does not end with EOI")
    position = len(_JPEG_START)
    while position < len(data):
        if data[position] != 0xFF:
            raise _malformed("JPEG", f"expected a marker at byte {position}")
        # Any number of 0xFF fill bytes may precede a marker code. The file ends
        # with EOI, so this stops on a marker code before running off the end.
        while data[position] == 0xFF:
            position += 1
        marker = data[position]
        position += 1
        if marker in _JPEG_STANDALONE_MARKERS:
            continue
        if marker == _JPEG_START_OF_SCAN or marker == _JPEG_END[1]:
            break
        length = int.from_bytes(data[position : position + 2], "big")
        if position + 2 > len(data) or length < 2 or position + length > len(data):
            raise _malformed("JPEG", f"segment at byte {position} overruns the file")
        if marker in _JPEG_FRAME_MARKERS:
            # Length(2), precision(1), height(2), width(2).
            if length < 7:
                raise _malformed("JPEG", "frame header is too short")
            height, width = struct.unpack(">HH", data[position + 3 : position + 7])
            return width, height
        position += length
    raise _malformed("JPEG", "no frame header before the image data")


def _webp_dimensions(data: bytes) -> tuple[int, int]:
    # RIFF(4) size(4) WEBP(4), then the first chunk: fourcc(4) size(4) payload.
    if len(data) < 20:
        raise _malformed("WebP", "file is too short")
    (riff_size,) = struct.unpack("<I", data[4:8])
    if riff_size + 8 != len(data):
        raise _malformed("WebP", "RIFF size does not match the file length")
    chunk_type, chunk_size = struct.unpack("<4sI", data[12:20])
    payload = data[20 : 20 + chunk_size]
    if len(payload) != chunk_size:
        raise _malformed("WebP", "first chunk overruns the file")
    if chunk_type == b"VP8 ":
        # Frame tag(3), start code 9d 01 2a, then 14-bit width and height.
        if chunk_size < 10 or payload[3:6] != b"\x9d\x01\x2a":
            raise _malformed("WebP", "lossy frame header is invalid")
        width, height = struct.unpack("<HH", payload[6:10])
        return width & 0x3FFF, height & 0x3FFF
    if chunk_type == b"VP8L":
        # Signature 0x2f, then width-1 and height-1 as 14-bit fields.
        if chunk_size < 5 or payload[0] != 0x2F:
            raise _malformed("WebP", "lossless header is invalid")
        (bits,) = struct.unpack("<I", payload[1:5])
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk_type == b"VP8X":
        # Flags(1), reserved(3), then canvas width-1 and height-1 as 24-bit fields.
        if chunk_size < 10:
            raise _malformed("WebP", "extended header is too short")
        width = int.from_bytes(payload[4:7], "little") + 1
        height = int.from_bytes(payload[7:10], "little") + 1
        return width, height
    raise _malformed("WebP", f"unknown first chunk {chunk_type!r}")
