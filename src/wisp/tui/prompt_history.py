"""Bounded, process-local prompt history for interactive TUI recall."""

from __future__ import annotations

from dataclasses import dataclass

PROMPT_HISTORY_CAPACITY = 100
PROMPT_HISTORY_PREVIEW_CHARS = 160


def _normalized_prompt_text(text: str) -> str:
    """Collapse display/search whitespace without changing stored prompt text."""

    return " ".join(text.split())


def _prompt_preview(prompt: str) -> str:
    normalized = _normalized_prompt_text(prompt)
    if len(normalized) <= PROMPT_HISTORY_PREVIEW_CHARS:
        return normalized
    return f"{normalized[: PROMPT_HISTORY_PREVIEW_CHARS - 1]}…"


@dataclass(frozen=True)
class PromptHistoryEntry:
    """One exact prompt plus bounded display metadata."""

    sequence: int
    prompt: str
    preview: str


def search_prompt_history(
    entries: tuple[PromptHistoryEntry, ...],
    query: str,
) -> tuple[PromptHistoryEntry, ...]:
    """Return deterministic newest-first literal substring matches."""

    normalized_query = _normalized_prompt_text(query).casefold()
    if not normalized_query:
        return entries
    return tuple(
        entry
        for entry in entries
        if normalized_query in _normalized_prompt_text(entry.prompt).casefold()
    )


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

        if not prompt.strip():
            return None
        self._entries = [entry for entry in self._entries if entry.prompt != prompt]
        entry = PromptHistoryEntry(
            sequence=self._next_sequence,
            prompt=prompt,
            preview=_prompt_preview(prompt),
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
    "PromptHistory",
    "PromptHistoryEntry",
    "search_prompt_history",
]
