from __future__ import annotations

import pytest

from wisp.providers.continuations import ContinuationStore


@pytest.mark.parametrize("capacity", [0, -1])
def test_continuation_store_requires_positive_capacity(capacity: int) -> None:
    with pytest.raises(ValueError, match="capacity must be positive"):
        ContinuationStore[str](capacity=capacity)


def test_continuation_store_remembers_gets_and_consumes_values() -> None:
    store = ContinuationStore[str](capacity=2)

    assert store.get(None) is None
    assert store.get("missing") is None
    assert store.consume(None) is None

    store.remember("response-1", "first")

    assert store.get("response-1") == "first"
    assert len(store) == 1
    assert store.consume("response-1") == "first"
    assert store.get("response-1") is None
    assert len(store) == 0


def test_continuation_store_discards_idempotently() -> None:
    store = ContinuationStore[str]()
    store.remember("response-1", "first")

    store.discard("response-1")
    store.discard("response-1")
    store.discard(None)

    assert store.get("response-1") is None


def test_continuation_store_evicts_the_oldest_entry() -> None:
    store = ContinuationStore[str](capacity=2)
    store.remember("response-1", "first")
    store.remember("response-2", "second")
    store.remember("response-3", "third")

    assert store.get("response-1") is None
    assert store.get("response-2") == "second"
    assert store.get("response-3") == "third"


def test_continuation_store_replacement_becomes_the_newest_entry() -> None:
    store = ContinuationStore[str](capacity=2)
    store.remember("response-1", "first")
    store.remember("response-2", "second")
    store.remember("response-1", "updated")
    store.remember("response-3", "third")

    assert store.get("response-1") == "updated"
    assert store.get("response-2") is None
    assert store.get("response-3") == "third"


def test_continuation_store_can_refresh_recency_on_get() -> None:
    store = ContinuationStore[str](capacity=2)
    store.remember("response-1", "first")
    store.remember("response-2", "second")

    assert store.get("response-1", refresh=True) == "first"
    store.remember("response-3", "third")

    assert store.get("response-1") == "first"
    assert store.get("response-2") is None
    assert store.get("response-3") == "third"


def test_continuation_store_get_does_not_refresh_recency_by_default() -> None:
    store = ContinuationStore[str](capacity=2)
    store.remember("response-1", "first")
    store.remember("response-2", "second")

    assert store.get("response-1") == "first"
    store.remember("response-3", "third")

    assert store.get("response-1") is None
    assert store.get("response-2") == "second"
    assert store.get("response-3") == "third"


def test_continuation_store_instances_are_isolated() -> None:
    first = ContinuationStore[str]()
    second = ContinuationStore[str]()
    first.remember("response-1", "first")

    assert first.get("response-1") == "first"
    assert second.get("response-1") is None
