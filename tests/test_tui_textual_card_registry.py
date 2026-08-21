from __future__ import annotations

import pytest

from wisp.tui.textual_card_registry import TextualCardIdentityRegistry
from wisp.tui.widgets import ProcessCard, ToolCard

pytestmark = pytest.mark.tui


def test_card_registry_rebinding_a_call_removes_the_prior_reverse_alias() -> None:
    registry = TextualCardIdentityRegistry()
    first = ToolCard("read", {"path": "one"})
    second = ToolCard("read", {"path": "two"})

    registry.bind_pending_call("call", first)
    registry.bind_pending_call("call", second)

    assert registry.pending_card("call") is second
    assert not registry.has_pending_calls(first)
    assert registry.has_pending_calls(second)
    assert registry.pending_call_count == 1


def test_card_registry_keeps_live_and_historical_process_cards_distinct() -> None:
    registry = TextualCardIdentityRegistry()
    live = ProcessCard("proc-1")
    historical = ProcessCard("proc-1", track_elapsed=False)

    registry.register_process_card("proc-1", live, historical=False)
    registry.mark_process_historical("proc-1", historical)

    assert registry.process_card("proc-1") is live
    assert registry.historical_process_card("proc-1") is historical
    assert registry.is_live_process_card(live)
    assert registry.is_historical(historical)
    assert not registry.is_historical(live)


def test_card_registry_forget_removes_every_identity_for_one_card() -> None:
    registry = TextualCardIdentityRegistry()
    card = ProcessCard("proc-1")
    registry.register_process_card("proc-1", card, historical=True)
    registry.bind_pending_call("poll-1", card)
    registry.bind_pending_call("poll-2", card)
    registry.bind_historical_tool_card("history:result", card)

    registry.forget(card)

    assert registry.pending_call_count == 0
    assert registry.process_card("proc-1") is None
    assert registry.historical_process_card("proc-1") is None
    assert registry.historical_tool_card("history:result") is None
    assert not registry.is_live_process_card(card)
    assert not registry.is_historical(card)
