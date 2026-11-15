"""The latest output of a background process, kept for the people watching it.

The agent reads a background process through ``bash poll``, which drains what it
returns, so the output the agent has read is gone from its buffer. A frontend that
shows a process's output reads this tail instead. The tail keeps the last bytes of
both streams in the order they arrived and is never drained, so reading it changes
nothing the agent's next poll returns.
"""

from __future__ import annotations

import codecs
from collections import deque
from dataclasses import dataclass
from typing import Final, Literal

from typing_extensions import TypeAliasType

ProcessStream = TypeAliasType("ProcessStream", Literal["stdout", "stderr"])

OUTPUT_TAIL_BYTES: Final = 32 * 1024
"""UTF-8 bytes of decoded output a process's tail keeps, across both streams."""
OUTPUT_TAIL_CHUNKS: Final = 1024
"""Chunks a tail keeps, so output switching streams constantly stays small too."""
# A chunk shorter than this takes the next text from its stream; a longer one starts
# a new chunk, so a busy stream never copies its whole tail on each read.
_JOIN_BELOW_CHARS: Final = 256


@dataclass(frozen=True, slots=True)
class OutputChunk:
    """A run of text one stream printed with nothing from the other in between.

    Attributes:
        stream (ProcessStream): Which stream printed it.
        text (str): The text, decoded as UTF-8 with invalid bytes replaced.
    """

    stream: ProcessStream
    text: str


@dataclass(frozen=True, slots=True)
class OutputTailSnapshot:
    """What a process's tail holds at one moment.

    Attributes:
        chunks (tuple[OutputChunk, ...]): The kept output, oldest first, with
            consecutive text from one stream joined.
        omitted_bytes (int): Bytes of output dropped from the front, across both
            streams.
    """

    chunks: tuple[OutputChunk, ...]
    omitted_bytes: int


class OutputTail:
    """Keep the last ``max_bytes`` of a process's output, both streams in order.

    Each stream decodes incrementally, so a character split across reads arrives
    whole. Text that does not fit, or that would exceed ``max_chunks`` chunks, is
    dropped from the front on a character boundary and counted in
    ``omitted_bytes``.
    """

    def __init__(
        self, max_bytes: int = OUTPUT_TAIL_BYTES, max_chunks: int = OUTPUT_TAIL_CHUNKS
    ) -> None:
        """Start an empty tail.

        Args:
            max_bytes (int, optional): UTF-8 bytes of decoded text to keep. Defaults
                to ``OUTPUT_TAIL_BYTES``.
            max_chunks (int, optional): Chunks to keep. Defaults to
                ``OUTPUT_TAIL_CHUNKS``.
        """

        self._max_bytes = max_bytes
        self._max_chunks = max_chunks
        self._decoders = {
            stream: codecs.getincrementaldecoder("utf-8")(errors="replace")
            for stream in ("stdout", "stderr")
        }
        self._chunks: deque[OutputChunk] = deque()
        self._kept_bytes = 0
        self._omitted_bytes = 0

    def append(self, stream: ProcessStream, data: bytes, *, final: bool = False) -> None:
        """Add the next bytes one stream printed.

        Args:
            stream (ProcessStream): The stream that printed them.
            data (bytes): Output in the order the stream produced it.
            final (bool, optional): Whether the stream has ended, so a character it
                left unfinished is decoded as a replacement. Defaults to False.
        """

        text = self._decoders[stream].decode(data, final=final)
        if not text:
            return
        added = len(text.encode("utf-8"))
        last = self._chunks[-1] if self._chunks else None
        if last is not None and last.stream == stream and len(last.text) < _JOIN_BELOW_CHARS:
            self._chunks[-1] = OutputChunk(stream, last.text + text)
        else:
            self._chunks.append(OutputChunk(stream, text))
        self._kept_bytes += added
        self._trim()

    def seed(self, stream: ProcessStream, data: bytes, dropped_bytes: int) -> None:
        """Start a stream with output printed before the tail existed.

        Only the end of ``data`` that fits in half the tail is kept, so seeding one
        stream never pushes the other's out.

        Args:
            stream (ProcessStream): The stream that printed it.
            data (bytes): The kept end of the stream, possibly ending mid-character.
            dropped_bytes (int): Bytes the stream printed before ``data``.
        """

        cut = max(0, len(data) - self._max_bytes // 2)
        while cut < len(data) and data[cut] & 0xC0 == 0x80:
            cut += 1
        self._omitted_bytes += dropped_bytes + cut
        self.append(stream, data[cut:])

    def snapshot(self) -> OutputTailSnapshot:
        """Return what the tail holds now, without changing it.

        Returns:
            OutputTailSnapshot: The kept chunks, oldest first, and the omitted bytes.
        """

        joined: list[OutputChunk] = []
        for chunk in self._chunks:
            if joined and joined[-1].stream == chunk.stream:
                joined[-1] = OutputChunk(chunk.stream, joined[-1].text + chunk.text)
            else:
                joined.append(chunk)
        return OutputTailSnapshot(chunks=tuple(joined), omitted_bytes=self._omitted_bytes)

    def _trim(self) -> None:
        while self._kept_bytes > self._max_bytes or len(self._chunks) > self._max_chunks:
            oldest = self._chunks[0]
            encoded = oldest.text.encode("utf-8")
            if len(self._chunks) > self._max_chunks:
                cut = len(encoded)
            else:
                cut = min(self._kept_bytes - self._max_bytes, len(encoded))
            # Cut on a character boundary so the kept text stays valid.
            while cut < len(encoded) and encoded[cut] & 0xC0 == 0x80:
                cut += 1
            self._kept_bytes -= cut
            self._omitted_bytes += cut
            if cut == len(encoded):
                self._chunks.popleft()
            else:
                self._chunks[0] = OutputChunk(oldest.stream, encoded[cut:].decode("utf-8"))
