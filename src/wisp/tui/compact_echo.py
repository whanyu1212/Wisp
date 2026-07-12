"""Bounded FIFO of compact transcript echoes for large-paste submissions.

The prompt channel and shell carry only the full submitted string. When a large
paste is expanded for the model, the transcript should still echo a compact
marker line rather than the whole blob. This log is the side map that lets
``prompt_submitted()`` recover that compact form without re-plumbing the queue.

It is a *per-prompt FIFO*, not a single value: submitting the same large paste
more than once (e.g. duplicate queued follow-ups) keeps a compact echo for each,
consumed in submission order. Insertion order is tracked globally so the map
stays bounded — a prompt abandoned before it echoes (cancel/quit/error/empty
-drop) never consumes its entry, so registration evicts the oldest past the cap,
and an interrupt/EOF clears the map wholesale.
"""

from __future__ import annotations

from collections import deque
from contextlib import suppress

# Hard cap on pending compact echoes. An echo is registered on Enter but consumed
# only when the prompt is actually echoed; a prompt abandoned before echo
# (cancelled/quit/errored/empty-dropped queued follow-up) would otherwise orphan
# its entry forever. The cap bounds the map so orphans can never accumulate — the
# oldest is evicted on overflow — and each key holds a whole pasted blob, so the
# bound must stay small.
MAX_PENDING_ECHOES = 32


class CompactEchoLog:
    """A bounded, per-prompt FIFO mapping full prompts to compact echo strings."""

    def __init__(self, max_pending: int = MAX_PENDING_ECHOES) -> None:
        self._max_pending = max_pending
        self._echoes: dict[str, deque[str]] = {}
        self._order: deque[str] = deque()

    @property
    def key_count(self) -> int:
        """Number of distinct prompts with at least one pending echo."""

        return len(self._echoes)

    @property
    def pending_count(self) -> int:
        """Total pending echoes across all prompts (bounded by the cap)."""

        return sum(len(queue) for queue in self._echoes.values())

    @property
    def order_length(self) -> int:
        """Length of the insertion-order marker deque (bounded by the cap)."""

        return len(self._order)

    def register(self, prompt: str, display: str) -> None:
        """Record a compact echo for ``prompt``, evicting the oldest past the cap.

        Appends to a per-prompt FIFO so duplicate submissions each keep an echo,
        tracking global insertion order so the map stays bounded: evict the oldest
        echo once the total exceeds the cap. An entry orphaned by an abandoned
        submission (never consumed) is thus reclaimed after enough newer pastes,
        and can never accumulate without bound.
        """

        self._echoes.setdefault(prompt, deque()).append(display)
        self._order.append(prompt)
        while len(self._order) > self._max_pending:
            oldest = self._order.popleft()
            queued = self._echoes.get(oldest)
            if queued:
                queued.popleft()
                if not queued:
                    del self._echoes[oldest]

    def clear(self) -> None:
        """Drop all pending compact echoes (the shell dropped its queued prompts).

        Called only on paths that actually abandon queued follow-ups, so their
        never-to-be-consumed echoes can't orphan (unbounded growth) or be popped
        by mistake by a later identical paste.
        """

        self._echoes.clear()
        self._order.clear()

    def take(self, prompt: str) -> str:
        """Return and consume the compact echo for a submitted prompt.

        Falls back to the prompt itself when no large-paste echo was registered
        (the common case). Each registered echo is single-use — consumed in
        submission order from the per-prompt FIFO — so N identical large pastes
        each echo compactly, and a later repeat with no fresh paste echoes verbatim.
        """

        echoes = self._echoes.get(prompt)
        if not echoes:
            return prompt
        display = echoes.popleft()
        if not echoes:
            del self._echoes[prompt]
        # Drop the matching insertion-order marker (the oldest for this key) so the
        # cap accounting stays exact and consumed echoes aren't evicted twice.
        with suppress(ValueError):
            self._order.remove(prompt)
        return display
