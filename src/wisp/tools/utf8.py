"""UTF-8 decoding helpers that preserve source-byte provenance."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DecodedTextUnit:
    """One decoded text unit and the number of source bytes it represents."""

    text: str
    source_bytes: int


def decode_utf8_units(data: bytes, *, final: bool) -> tuple[tuple[DecodedTextUnit, ...], bytes]:
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
            units.append(DecodedTextUnit(text="\ufffd", source_bytes=1))
            index += 1
            continue

        if index + expected > len(data):
            try:
                text = data[index:].decode("utf-8")
            except UnicodeDecodeError as exc:
                if not final and exc.reason == "unexpected end of data":
                    return tuple(units), data[index:]
                invalid_bytes = max(1, exc.end)
                units.append(DecodedTextUnit(text="\ufffd", source_bytes=invalid_bytes))
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
            units.append(DecodedTextUnit(text="\ufffd", source_bytes=invalid_bytes))
            index += invalid_bytes
            continue

        units.append(DecodedTextUnit(text=text, source_bytes=expected))
        index += expected

    return tuple(units), b""


def decode_utf8_with_source_byte_lengths(data: bytes) -> tuple[str, tuple[int, ...]]:
    units, _pending = decode_utf8_units(data, final=True)
    return (
        "".join(unit.text for unit in units),
        tuple(unit.source_bytes for unit in units),
    )


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
