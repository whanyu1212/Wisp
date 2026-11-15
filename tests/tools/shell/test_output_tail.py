"""The latest-output tail frontends read from a background process."""

from __future__ import annotations

import pytest

from wisp.tools.shell.output_tail import OutputChunk, OutputTail


def test_streams_keep_arrival_order() -> None:
    """Consecutive output from one stream joins; a switch starts a new chunk."""

    tail = OutputTail()
    for stream, data in [
        ("stdout", b"build "),
        ("stdout", b"started\n"),
        ("stderr", b"warning\n"),
        ("stdout", b"done\n"),
    ]:
        tail.append(stream, data)  # type: ignore[arg-type]

    snapshot = tail.snapshot()

    assert snapshot.chunks == (
        OutputChunk("stdout", "build started\n"),
        OutputChunk("stderr", "warning\n"),
        OutputChunk("stdout", "done\n"),
    )
    assert snapshot.omitted_bytes == 0


@pytest.mark.parametrize("text", ["0123456789abcdef", "héllo wörld ünïcode"])
def test_trim_keeps_latest_whole_characters(text: str) -> None:
    """Old text drops on a character boundary, and every byte is kept or counted."""

    tail = OutputTail(max_bytes=7)
    for character in text:
        tail.append("stdout", character.encode("utf-8"))

    snapshot = tail.snapshot()
    kept = "".join(chunk.text for chunk in snapshot.chunks)

    assert text.endswith(kept)
    # As much as fits: at most one partly fitting character is dropped too.
    assert 7 - 3 <= len(kept.encode("utf-8")) <= 7
    assert len(kept.encode("utf-8")) + snapshot.omitted_bytes == len(text.encode("utf-8"))


def test_trim_drops_whole_old_chunks() -> None:
    tail = OutputTail(max_bytes=4)
    tail.append("stderr", b"old\n")
    tail.append("stdout", b"new\n")

    assert tail.snapshot().chunks == (OutputChunk("stdout", "new\n"),)
    assert tail.snapshot().omitted_bytes == 4


def test_trim_leaves_no_empty_chunk() -> None:
    """A cut inside a chunk's last character drops the whole chunk."""

    tail = OutputTail(max_bytes=1)
    tail.append("stdout", "é".encode())

    assert tail.snapshot().chunks == ()
    assert tail.snapshot().omitted_bytes == 2


def test_split_character_joins_across_reads() -> None:
    """A character cut between reads arrives whole; one cut off at the end is replaced."""

    encoded = "é".encode()
    tail = OutputTail()
    tail.append("stdout", encoded[:1])
    assert tail.snapshot().chunks == ()
    tail.append("stdout", encoded[1:])
    tail.append("stderr", encoded[:1], final=True)

    assert tail.snapshot().chunks == (
        OutputChunk("stdout", "é"),
        OutputChunk("stderr", "�"),
    )


def test_seed_keeps_end_of_each_stream() -> None:
    """Output from before the tail keeps the end of each stream, half the tail each."""

    tail = OutputTail(max_bytes=8)
    tail.seed("stdout", b"0123456789", dropped_bytes=5)
    tail.seed("stderr", "abcdé".encode(), dropped_bytes=0)

    snapshot = tail.snapshot()

    assert snapshot.chunks == (OutputChunk("stdout", "6789"), OutputChunk("stderr", "cdé"))
    # 5 dropped earlier, 6 cut from stdout, 2 cut from stderr.
    assert snapshot.omitted_bytes == 13


def test_chunk_count_is_capped() -> None:
    """Output that switches streams constantly still keeps a bounded number of chunks."""

    tail = OutputTail(max_chunks=4)
    for index in range(10):
        tail.append("stdout" if index % 2 else "stderr", str(index).encode())

    snapshot = tail.snapshot()

    assert [chunk.text for chunk in snapshot.chunks] == ["6", "7", "8", "9"]
    assert snapshot.omitted_bytes == 6


def test_long_chunks_stay_whole_in_snapshot() -> None:
    """A busy stream's text, kept in several pieces, reads back as one chunk."""

    tail = OutputTail()
    for _ in range(4):
        tail.append("stdout", b"x" * 300)

    assert tail.snapshot().chunks == (OutputChunk("stdout", "x" * 1200),)
