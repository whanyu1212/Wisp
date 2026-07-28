"""Deterministic command-palette search independent of Textual widgets."""

from __future__ import annotations

from dataclasses import dataclass

from wisp.runtime.commands import CommandDescriptor
from wisp.tui.commands import TuiCommandCatalog


@dataclass(frozen=True)
class CommandPaletteMatch:
    """One command descriptor matched by a palette query."""

    descriptor: CommandDescriptor
    score: tuple[int, int, int]


def search_command_catalog(
    catalog: TuiCommandCatalog,
    query: str,
) -> tuple[CommandPaletteMatch, ...]:
    """Return deterministic fuzzy matches for ``query``."""

    tokens = tuple(part for part in query.casefold().split() if part)
    if not tokens:
        return tuple(
            CommandPaletteMatch(descriptor, (0, 0, descriptor.order))
            for descriptor in catalog.descriptors
        )

    matches: list[CommandPaletteMatch] = []
    for descriptor in catalog.descriptors:
        fields = _search_fields(descriptor)
        token_scores: list[tuple[int, int, int]] = []
        for token in tokens:
            candidates = [
                score
                for priority, value in fields
                if (score := _score_token(token, value, priority)) is not None
            ]
            if not candidates:
                break
            token_scores.append(min(candidates))
        else:
            matches.append(
                CommandPaletteMatch(
                    descriptor,
                    (
                        sum(score[0] for score in token_scores),
                        sum(score[1] for score in token_scores),
                        min(score[2] for score in token_scores),
                    ),
                )
            )

    return tuple(
        sorted(
            matches,
            key=lambda match: (
                match.score,
                match.descriptor.order,
                match.descriptor.name,
            ),
        )
    )


def _search_fields(descriptor: CommandDescriptor) -> tuple[tuple[int, str], ...]:
    aliases = tuple(alias.casefold() for alias in (*descriptor.aliases, *descriptor.slash_aliases))
    normalized_aliases = tuple(alias.removeprefix("/") for alias in aliases)
    return (
        (0, descriptor.name.casefold()),
        (0, descriptor.slash_command.casefold()),
        *((1, alias) for alias in (*aliases, *normalized_aliases)),
        (2, descriptor.title.casefold()),
        (3, str(descriptor.category).casefold()),
        (4, descriptor.description.casefold()),
    )


def _score_token(token: str, value: str, priority: int) -> tuple[int, int, int] | None:
    if token == value:
        return (0, 0, priority)
    if value.startswith(token):
        return (1, len(value) - len(token), priority)
    position = value.find(token)
    if position >= 0:
        return (2, position + len(value) - len(token), priority)
    gap = _subsequence_gap(token, value)
    if gap is None:
        return None
    return (3, gap, priority)


def _subsequence_gap(needle: str, haystack: str) -> int | None:
    positions: list[int] = []
    cursor = 0
    for character in needle:
        position = haystack.find(character, cursor)
        if position < 0:
            return None
        positions.append(position)
        cursor = position + 1
    return positions[-1] - positions[0] + 1 - len(needle)


__all__ = ["CommandPaletteMatch", "search_command_catalog"]
