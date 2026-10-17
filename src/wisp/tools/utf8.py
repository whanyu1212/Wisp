"""UTF-8 decoding helpers that preserve source-byte provenance."""

from __future__ import annotations

import codecs
from dataclasses import dataclass

# Maps each UTF-8 lead byte to the length of the sequence it starts. Applied only to
# strictly valid UTF-8 with continuation bytes deleted, so every remaining byte is the
# lead of exactly one decoded character.
_LEAD_BYTE_SEQUENCE_LENGTHS = bytes(
    1 if byte < 0x80 else 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4 for byte in range(256)
)
_CONTINUATION_BYTES = bytes(range(0x80, 0xC0))


@dataclass(frozen=True)
class DecodedTextUnit:
    """One decoded text unit and the number of source bytes it represents."""

    text: str
    source_bytes: int


def decode_utf8_units(data: bytes, *, final: bool) -> tuple[tuple[DecodedTextUnit, ...], bytes]:
    """Decode UTF-8 into single-character units that record their source byte counts.

    Each invalid sequence becomes one U+FFFD unit covering the bytes it replaced, so
    the units' ``source_bytes`` always sum to the number of bytes consumed.

    Args:
        data (bytes): Raw bytes to decode.
        final (bool): Whether no more bytes follow. When false, an incomplete but
            still valid trailing sequence is returned as pending instead of replaced.

    Returns:
        tuple[tuple[DecodedTextUnit, ...], bytes]: The decoded units and the pending
        trailing bytes to prepend to the next chunk (always empty when ``final``).
    """

    text, source_byte_lengths, pending = _decode_utf8(data, final=final)
    return tuple(map(DecodedTextUnit, text, source_byte_lengths)), pending


def decode_utf8_with_source_byte_lengths(data: bytes) -> tuple[str, tuple[int, ...]]:
    """Decode complete UTF-8 output and report each character's source byte count.

    Args:
        data (bytes): Raw bytes to decode; a truncated trailing sequence is replaced.

    Returns:
        tuple[str, tuple[int, ...]]: The decoded text and, for each of its characters,
        the number of source bytes it represents. The lengths sum to ``len(data)``.
    """

    text, source_byte_lengths, _pending = _decode_utf8(data, final=True)
    return text, source_byte_lengths


def _decode_utf8(data: bytes, *, final: bool) -> tuple[str, tuple[int, ...], bytes]:
    if data.isascii():
        return data.decode("ascii"), (1,) * len(data), b""

    # Decode the longest strictly valid prefix in C. Every byte after it goes through
    # the per-sequence path, which also owns pending and truncated trailing sequences.
    try:
        prefix_text, prefix_bytes = codecs.utf_8_decode(data, "strict", False)
    except UnicodeDecodeError as exc:
        prefix_bytes = exc.start
        prefix_text = data[:prefix_bytes].decode("utf-8")
    prefix_lengths = data[:prefix_bytes].translate(_LEAD_BYTE_SEQUENCE_LENGTHS, _CONTINUATION_BYTES)
    if prefix_bytes == len(data):
        return prefix_text, tuple(prefix_lengths), b""

    suffix_units, pending = _decode_utf8_per_sequence(data[prefix_bytes:], final=final)
    return (
        prefix_text + "".join(unit.text for unit in suffix_units),
        (*prefix_lengths, *(unit.source_bytes for unit in suffix_units)),
        pending,
    )


def _decode_utf8_per_sequence(
    data: bytes, *, final: bool
) -> tuple[tuple[DecodedTextUnit, ...], bytes]:
    # Reference decoder. It never looks behind its index, so running it on a suffix
    # that starts at a character boundary matches running it on the whole input.
    units: list[DecodedTextUnit] = []
    index = 0
    while index < len(data):
        first = data[index]
        if first < 0x80:
            units.append(DecodedTextUnit(text=chr(first), source_bytes=1))
            index += 1
            continue

        expected = _utf8_sequence_length(first)
        if expected is None:
            units.append(DecodedTextUnit(text="�", source_bytes=1))
            index += 1
            continue

        if index + expected > len(data):
            try:
                text = data[index:].decode("utf-8")
            except UnicodeDecodeError as exc:
                if not final and exc.reason == "unexpected end of data":
                    return tuple(units), data[index:]
                invalid_bytes = max(1, exc.end)
                units.append(DecodedTextUnit(text="�", source_bytes=invalid_bytes))
                index += invalid_bytes
                continue
            if final:
                units.append(DecodedTextUnit(text=text, source_bytes=len(data) - index))
                return tuple(units), b""
            return tuple(units), data[index:]

        sequence = data[index : index + expected]
        try:
            text = sequence.decode("utf-8")
        except UnicodeDecodeError as exc:
            invalid_bytes = max(1, exc.end)
            units.append(DecodedTextUnit(text="�", source_bytes=invalid_bytes))
            index += invalid_bytes
            continue

        units.append(DecodedTextUnit(text=text, source_bytes=expected))
        index += expected

    return tuple(units), b""


def _utf8_sequence_length(first: int) -> int | None:
    if 0xC2 <= first <= 0xDF:
        return 2
    if 0xE0 <= first <= 0xEF:
        return 3
    if 0xF0 <= first <= 0xF4:
        return 4
    return None


__all__ = [
    "DecodedTextUnit",
    "decode_utf8_units",
    "decode_utf8_with_source_byte_lengths",
]
