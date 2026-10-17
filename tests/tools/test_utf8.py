from __future__ import annotations

import itertools
import random

import pytest

from wisp.tools.utf8 import (
    DecodedTextUnit,
    decode_utf8_units,
    decode_utf8_with_source_byte_lengths,
)

# Bytes at every UTF-8 boundary: ASCII, continuation ranges, overlong and surrogate
# leads, the last valid four-byte lead, and bytes that never appear in UTF-8.
_BOUNDARY_BYTES = (
    0x00,
    0x41,
    0x7F,
    0x80,
    0x8F,
    0x90,
    0x9F,
    0xA0,
    0xBF,
    0xC0,
    0xC1,
    0xC2,
    0xDF,
    0xE0,
    0xE1,
    0xEC,
    0xED,
    0xEE,
    0xEF,
    0xF0,
    0xF1,
    0xF3,
    0xF4,
    0xF5,
    0xFF,
)
_FOUR_BYTE_SAMPLE = (0x41, 0x80, 0x8F, 0x90, 0xBF, 0xE0, 0xF0, 0xF4, 0xF5)
# An empty prefix starts at the per-sequence path; a non-ASCII prefix first takes the
# valid-prefix fast path and hands the remaining bytes over mid-input.
_PREFIXES = (b"", "😀".encode())


def _reference_decode_utf8_units(
    data: bytes, *, final: bool
) -> tuple[tuple[DecodedTextUnit, ...], bytes]:
    """Frozen copy of the per-sequence decoder that predates the fast paths."""

    units: list[DecodedTextUnit] = []
    index = 0
    while index < len(data):
        first = data[index]
        if first < 0x80:
            units.append(DecodedTextUnit(text=chr(first), source_bytes=1))
            index += 1
            continue

        if 0xC2 <= first <= 0xDF:
            expected = 2
        elif 0xE0 <= first <= 0xEF:
            expected = 3
        elif 0xF0 <= first <= 0xF4:
            expected = 4
        else:
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


def _assert_matches_reference(data: bytes) -> None:
    for final in (True, False):
        expected = _reference_decode_utf8_units(data, final=final)
        assert decode_utf8_units(data, final=final) == expected, (data, final)

    units, _pending = _reference_decode_utf8_units(data, final=True)
    assert decode_utf8_with_source_byte_lengths(data) == (
        "".join(unit.text for unit in units),
        tuple(unit.source_bytes for unit in units),
    ), data


def test_decoding_empty_input_returns_no_units() -> None:
    assert decode_utf8_units(b"", final=True) == ((), b"")
    assert decode_utf8_units(b"", final=False) == ((), b"")
    assert decode_utf8_with_source_byte_lengths(b"") == ("", ())


def test_decoding_reports_source_bytes_per_character() -> None:
    assert decode_utf8_with_source_byte_lengths("aé中😀".encode()) == ("aé中😀", (1, 2, 3, 4))
    assert decode_utf8_units(b"a\xe4\xb8", final=False) == (
        (DecodedTextUnit(text="a", source_bytes=1),),
        b"\xe4\xb8",
    )
    assert decode_utf8_with_source_byte_lengths(b"a\xe4\xb8") == ("a�", (1, 2))
    assert decode_utf8_with_source_byte_lengths(b"\xe2\x82A\xffb") == (
        "�A�b",
        (2, 1, 1, 1),
    )


@pytest.mark.parametrize("prefix", _PREFIXES)
def test_decoding_matches_reference_for_every_one_and_two_byte_input(prefix: bytes) -> None:
    for first in range(256):
        _assert_matches_reference(prefix + bytes((first,)))
        for second in range(256):
            _assert_matches_reference(prefix + bytes((first, second)))


@pytest.mark.parametrize("prefix", _PREFIXES)
def test_decoding_matches_reference_for_boundary_byte_sequences(prefix: bytes) -> None:
    for sequence in itertools.product(_BOUNDARY_BYTES, repeat=3):
        _assert_matches_reference(prefix + bytes(sequence))
        _assert_matches_reference(prefix + bytes(sequence) + "é".encode())
    for sequence in itertools.product(_FOUR_BYTE_SAMPLE, repeat=4):
        _assert_matches_reference(prefix + bytes(sequence))


def test_decoding_matches_reference_for_random_mixed_input() -> None:
    rng = random.Random(20261017)
    pieces = ("a", "\n", "é", "ß", "中", "€", "😀", "\U0010ffff", "�")
    for _ in range(300):
        parts: list[bytes] = []
        for _ in range(rng.randrange(0, 60)):
            if rng.random() < 0.1:
                parts.append(rng.randbytes(rng.randrange(1, 4)))
            else:
                parts.append(rng.choice(pieces).encode())
        data = b"".join(parts)
        _assert_matches_reference(data)
        # Cutting anywhere exercises truncated trailing sequences at every position.
        _assert_matches_reference(data[: rng.randrange(0, len(data) + 1)])


def test_incremental_decoding_matches_whole_input_decoding() -> None:
    data = ("ascii é 中文 😀 " * 50).encode() + b"\xff\xe2\x82 tail"
    for chunk_size in (1, 2, 3, 5, 7):
        units: list[DecodedTextUnit] = []
        pending = b""
        for start in range(0, len(data), chunk_size):
            chunk_units, pending = decode_utf8_units(
                pending + data[start : start + chunk_size], final=False
            )
            units.extend(chunk_units)
        final_units, pending = decode_utf8_units(pending, final=True)
        units.extend(final_units)

        assert pending == b""
        assert tuple(units) == _reference_decode_utf8_units(data, final=True)[0]
