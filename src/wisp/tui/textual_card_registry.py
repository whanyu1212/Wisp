"""Typed card identity and ownership for the Textual transcript.

The registry owns identity relationships only. Mounting, rendering, retention, and
persisted-history ordering remain with their existing controllers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from wisp.tui.widgets import ProcessCard, ToolCard


@dataclass(slots=True)
class _CardIdentity:
    """All reverse aliases and ownership flags for one mounted card."""

    pending_call_ids: set[str] = field(default_factory=set)
    historical_card_ids: set[str] = field(default_factory=set)
    process_id: str | None = None
    historical_process_id: str | None = None
    live_process: bool = False
    history_owned: bool = False


class TextualCardIdentityRegistry:
    """Track call aliases and live/history ownership for mounted cards."""

    def __init__(self) -> None:
        self._identities: dict[ToolCard, _CardIdentity] = {}
        self._pending_calls: dict[str, ToolCard] = {}
        self._process_cards: dict[str, ProcessCard] = {}
        self._historical_process_cards: dict[str, ProcessCard] = {}
        self._historical_tool_cards: dict[str, ToolCard] = {}

    @property
    def pending_call_count(self) -> int:
        return len(self._pending_calls)

    def bind_pending_call(self, call_id: str, card: ToolCard) -> None:
        prior = self._pending_calls.get(call_id)
        if prior is card:
            return
        if prior is not None:
            self._identity(prior).pending_call_ids.discard(call_id)
        self._pending_calls[call_id] = card
        self._identity(card).pending_call_ids.add(call_id)

    def pending_card(self, call_id: str) -> ToolCard | None:
        return self._pending_calls.get(call_id)

    def pop_pending_call(self, call_id: str) -> ToolCard | None:
        card = self._pending_calls.pop(call_id, None)
        if card is not None:
            self._identity(card).pending_call_ids.discard(call_id)
        return card

    def has_pending_calls(self, card: ToolCard) -> bool:
        identity = self._identities.get(card)
        return identity is not None and bool(identity.pending_call_ids)

    def pending_cards(self) -> tuple[ToolCard, ...]:
        return tuple(
            card for card, identity in self._identities.items() if identity.pending_call_ids
        )

    def process_card(self, process_id: str) -> ProcessCard | None:
        return self._process_cards.get(process_id)

    def historical_process_card(self, process_id: str) -> ProcessCard | None:
        return self._historical_process_cards.get(process_id)

    def register_process_card(
        self,
        process_id: str,
        card: ProcessCard,
        *,
        historical: bool,
    ) -> None:
        prior_card = self._process_cards.get(process_id)
        if prior_card is not None and prior_card is not card:
            self._identity(prior_card).process_id = None
        identity = self._identity(card)
        if identity.process_id is not None and identity.process_id != process_id:
            self._process_cards.pop(identity.process_id, None)
        self._process_cards[process_id] = card
        identity.process_id = process_id
        if historical:
            self.mark_process_historical(process_id, card)
        else:
            self.mark_process_live(process_id, card)

    def mark_process_historical(self, process_id: str, card: ProcessCard) -> None:
        prior_card = self._historical_process_cards.get(process_id)
        if prior_card is not None and prior_card is not card:
            prior_identity = self._identity(prior_card)
            prior_identity.historical_process_id = None
        identity = self._identity(card)
        if (
            identity.historical_process_id is not None
            and identity.historical_process_id != process_id
        ):
            self._historical_process_cards.pop(identity.historical_process_id, None)
        self._historical_process_cards[process_id] = card
        identity.historical_process_id = process_id

    def mark_process_live(self, process_id: str, card: ProcessCard) -> None:
        identity = self._identity(card)
        if self._historical_process_cards.get(process_id) is card:
            del self._historical_process_cards[process_id]
            identity.historical_process_id = None
        identity.history_owned = False
        identity.live_process = True

    def is_live_process_card(self, card: ProcessCard) -> bool:
        identity = self._identities.get(card)
        return identity is not None and identity.live_process

    def bind_historical_tool_card(self, card_id: str, card: ToolCard) -> None:
        prior = self._historical_tool_cards.get(card_id)
        if prior is card:
            return
        if prior is not None:
            prior_identity = self._identity(prior)
            prior_identity.historical_card_ids.discard(card_id)
        self._historical_tool_cards[card_id] = card
        identity = self._identity(card)
        identity.historical_card_ids.add(card_id)

    def historical_tool_card(self, card_id: str) -> ToolCard | None:
        return self._historical_tool_cards.get(card_id)

    def mark_historical(self, card: ToolCard) -> None:
        self._identity(card).history_owned = True

    def is_historical(self, card: ToolCard) -> bool:
        identity = self._identities.get(card)
        return identity is not None and (
            identity.history_owned
            or bool(identity.historical_card_ids)
            or identity.historical_process_id is not None
        )

    def forget(self, card: ToolCard) -> None:
        identity = self._identities.pop(card, None)
        if identity is None:
            return
        for call_id in identity.pending_call_ids:
            self._pending_calls.pop(call_id, None)
        for card_id in identity.historical_card_ids:
            self._historical_tool_cards.pop(card_id, None)
        if isinstance(card, ProcessCard):
            if identity.historical_process_id is not None:
                self._historical_process_cards.pop(identity.historical_process_id, None)
            if identity.process_id is not None:
                self._process_cards.pop(identity.process_id, None)

    def clear_pending_calls(self) -> None:
        self._pending_calls.clear()
        for identity in self._identities.values():
            identity.pending_call_ids.clear()

    def clear(self) -> None:
        self._identities.clear()
        self._pending_calls.clear()
        self._process_cards.clear()
        self._historical_process_cards.clear()
        self._historical_tool_cards.clear()

    def _identity(self, card: ToolCard) -> _CardIdentity:
        identity = self._identities.get(card)
        if identity is None:
            identity = _CardIdentity()
            self._identities[card] = identity
        return identity


__all__ = ["TextualCardIdentityRegistry"]
