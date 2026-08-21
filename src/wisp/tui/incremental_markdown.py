"""Conservative stable-prefix reuse for append-only Rich Markdown streams.

Rich exposes its parsed token list as part of the Markdown renderable. This module
contains the single compatibility seam that reuses those tokens. Any unfamiliar
shape falls back to a complete parse so parser internals can never compromise
rendering correctness.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

_REFERENCE_DEFINITION_RE = re.compile(r"(?m)^[ ]{0,3}\[[^\]\n]+\]:[ \t]*(?:\S|$)")


class _ParsedMarkdown(Protocol):
    parsed: Sequence[object]


@dataclass(frozen=True, slots=True)
class IncrementalMarkdownBuild[MarkdownT]:
    """One parsed document plus numeric work and stable-fence metadata."""

    markdown: MarkdownT
    processed_chars: int
    reused_chars: int
    cacheable_fence_token_ids: frozenset[int]
    incremental: bool


class IncrementalMarkdownState:
    """Retain complete top-level blocks while reparsing only the open tail."""

    def __init__(self) -> None:
        self._stable_source_chars = 0
        self._stable_tokens: list[object] = []
        self._stable_fence_token_ids: set[int] = set()
        self._full_rebuild_only = False

    def reset(self) -> None:
        """Discard all parser state after replacement or presentation changes."""

        self._stable_source_chars = 0
        self._stable_tokens.clear()
        self._stable_fence_token_ids.clear()
        self._full_rebuild_only = False

    def release(self) -> None:
        """Release streaming-only token references after the turn settles."""

        self.reset()

    def build[MarkdownT](
        self,
        source: str,
        build_markdown: Callable[[str], MarkdownT],
    ) -> IncrementalMarkdownBuild[MarkdownT]:
        """Build a combined document, failing closed to a complete source parse."""

        if self._stable_source_chars > len(source):
            self.reset()
        mutable_source = source[self._stable_source_chars :]
        if _REFERENCE_DEFINITION_RE.search(mutable_source):
            # A late reference definition can change links in an already stable
            # prefix. Keep such uncommon documents on Rich's complete parse path.
            self._full_rebuild_only = True
            self._stable_source_chars = 0
            self._stable_tokens.clear()
            self._stable_fence_token_ids.clear()

        if self._full_rebuild_only:
            markdown = build_markdown(source)
            return IncrementalMarkdownBuild(
                markdown=markdown,
                processed_chars=len(source),
                reused_chars=0,
                cacheable_fence_token_ids=frozenset(),
                incremental=False,
            )

        reused_chars = self._stable_source_chars
        mutable_source = source[reused_chars:]
        markdown = build_markdown(mutable_source)
        try:
            parsed = _parsed_tokens(markdown)
            token_cut, stable_tail_chars = _stable_token_cut(parsed, mutable_source)
            newly_stable = parsed[:token_cut]
            combined = [*self._stable_tokens, *newly_stable, *parsed[token_cut:]]
            # Rich 15 parses in its constructor and renders from ``parsed``. Keep
            # that compatibility seam guarded with the token-shape inspection.
            cast(_ParsedMarkdown, markdown).parsed = combined
        except (AttributeError, TypeError, ValueError):
            # Rich or markdown-it changed shape. Disable optimization for this
            # document and immediately rebuild from the authoritative source.
            self._full_rebuild_only = True
            self._stable_source_chars = 0
            self._stable_tokens.clear()
            self._stable_fence_token_ids.clear()
            markdown = build_markdown(source)
            return IncrementalMarkdownBuild(
                markdown=markdown,
                processed_chars=len(mutable_source) + len(source),
                reused_chars=0,
                cacheable_fence_token_ids=frozenset(),
                incremental=False,
            )

        self._stable_tokens.extend(newly_stable)
        self._stable_fence_token_ids.update(
            id(token) for token in newly_stable if getattr(token, "type", None) == "fence"
        )
        self._stable_source_chars += stable_tail_chars
        return IncrementalMarkdownBuild(
            markdown=markdown,
            processed_chars=len(mutable_source),
            reused_chars=reused_chars,
            cacheable_fence_token_ids=frozenset(self._stable_fence_token_ids),
            incremental=True,
        )


def _parsed_tokens(markdown: object) -> tuple[object, ...]:
    parsed = cast(_ParsedMarkdown, markdown).parsed
    if not isinstance(parsed, Sequence) or isinstance(parsed, str | bytes):
        raise TypeError("Rich Markdown parsed tokens are not a sequence")
    return tuple(parsed)


def _stable_token_cut(tokens: tuple[object, ...], source: str) -> tuple[int, int]:
    """Return the token and source cut before the final top-level block."""

    groups: list[tuple[int, int]] = []
    depth = 0
    group_start = 0
    group_end_line = 0
    for index, token in enumerate(tokens):
        nesting = getattr(token, "nesting", None)
        if not isinstance(nesting, int) or nesting not in {-1, 0, 1}:
            raise TypeError("Markdown token nesting is unavailable")
        if depth == 0:
            group_start = index
            group_end_line = 0
        token_map = getattr(token, "map", None)
        if token_map is not None:
            if (
                not isinstance(token_map, Sequence)
                or isinstance(token_map, str | bytes)
                or len(token_map) != 2
                or not all(isinstance(value, int) for value in token_map)
            ):
                raise TypeError("Markdown token source map is unavailable")
            group_end_line = max(group_end_line, int(token_map[1]))
        depth += nesting
        if depth < 0:
            raise ValueError("Markdown token nesting became negative")
        if depth == 0:
            if group_end_line < 1:
                raise ValueError("Markdown top-level block has no source map")
            groups.append((index + 1, group_end_line))
            group_start = index + 1
    if depth != 0 or group_start != len(tokens):
        raise ValueError("Markdown token nesting is incomplete")
    if len(groups) < 2:
        return 0, 0

    token_cut, stable_end_line = groups[-2]
    line_offsets = [0]
    for line in source.splitlines(keepends=True):
        line_offsets.append(line_offsets[-1] + len(line))
    if stable_end_line >= len(line_offsets):
        raise ValueError("Markdown token source map exceeds source lines")
    return token_cut, line_offsets[stable_end_line]


__all__ = ["IncrementalMarkdownBuild", "IncrementalMarkdownState"]
