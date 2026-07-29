"""Bounded, process-local prompt history for interactive TUI recall."""

from __future__ import annotations

from dataclasses import dataclass, field

PROMPT_HISTORY_CAPACITY = 100
PROMPT_HISTORY_PREVIEW_CHARS = 160
PROMPT_HISTORY_SEARCH_CHARS = 16_384


def _normalized_prompt_prefix(text: str, *, limit: int) -> str:
    """Collapse whitespace into a bounded prefix without copying the full prompt."""

    normalized: list[str] = []
    pending_space = False
    for character in text:
        if character.isspace():
            if normalized:
                pending_space = True
            continue
        if pending_space:
            if len(normalized) >= limit:
                break
            normalized.append(" ")
            pending_space = False
        if len(normalized) >= limit:
            break
        normalized.append(character)
        if len(normalized) >= limit:
            break
    return "".join(normalized)


@dataclass(frozen=True)
class PromptHistoryEntry:
    """One exact prompt plus bounded display metadata."""

    sequence: int
    prompt: str
    preview: str
    search_text: str = field(repr=False)


def search_prompt_history(
    entries: tuple[PromptHistoryEntry, ...],
    query: str,
) -> tuple[PromptHistoryEntry, ...]:
    """Return deterministic newest-first literal substring matches."""

    normalized_query = _normalized_prompt_prefix(
        query,
        limit=PROMPT_HISTORY_SEARCH_CHARS,
    ).casefold()
    if not normalized_query:
        return entries
    return tuple(entry for entry in entries if normalized_query in entry.search_text)


class PromptHistory:
    """A bounded unique-MRU of prompts retained only by the current process."""

    def __init__(self, capacity: int = PROMPT_HISTORY_CAPACITY) -> None:
        if capacity <= 0:
            raise ValueError("prompt history capacity must be positive")
        self._capacity = capacity
        self._entries: list[PromptHistoryEntry] = []
        self._next_sequence = 1

    @property
    def entries(self) -> tuple[PromptHistoryEntry, ...]:
        """Return entries newest first."""

        return tuple(self._entries)

    def record(self, prompt: str) -> PromptHistoryEntry | None:
        """Record a non-blank exact prompt, moving duplicates to newest."""

        normalized = _normalized_prompt_prefix(
            prompt,
            limit=PROMPT_HISTORY_SEARCH_CHARS,
        )
        if not normalized:
            return None
        self._entries = [entry for entry in self._entries if entry.prompt != prompt]
        preview = normalized
        if len(preview) > PROMPT_HISTORY_PREVIEW_CHARS:
            preview = f"{preview[: PROMPT_HISTORY_PREVIEW_CHARS - 1]}…"
        entry = PromptHistoryEntry(
            sequence=self._next_sequence,
            prompt=prompt,
            preview=preview,
            search_text=normalized.casefold(),
        )
        self._next_sequence += 1
        self._entries.insert(0, entry)
        del self._entries[self._capacity :]
        return entry

    def search(self, query: str) -> tuple[PromptHistoryEntry, ...]:
        return search_prompt_history(self.entries, query)

    def clear(self) -> None:
        self._entries.clear()
        self._next_sequence = 1


__all__ = [
    "PROMPT_HISTORY_CAPACITY",
    "PROMPT_HISTORY_PREVIEW_CHARS",
    "PROMPT_HISTORY_SEARCH_CHARS",
    "PromptHistory",
    "PromptHistoryEntry",
    "search_prompt_history",
]
