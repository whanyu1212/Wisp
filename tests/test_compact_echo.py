"""Unit tests for the compact-echo FIFO extracted from the Textual app.

The integration tests exercise this indirectly through the app; these pin the
data structure's contract directly (FIFO order, duplicate prompts, bounded cap).
"""

from __future__ import annotations

from wisp.tui.compact_echo import MAX_PENDING_ECHOES, CompactEchoLog


def test_take_falls_back_to_the_prompt_when_no_echo_registered() -> None:
    log = CompactEchoLog()
    assert log.take("hello") == "hello"  # common case: no large paste


def test_register_then_take_returns_the_compact_display_once() -> None:
    log = CompactEchoLog()
    log.register("full prompt", "[Pasted #1]")
    assert log.take("full prompt") == "[Pasted #1]"  # consumed
    assert log.take("full prompt") == "full prompt"  # single-use, then falls back


def test_duplicate_prompts_keep_one_echo_each_consumed_in_order() -> None:
    log = CompactEchoLog()
    log.register("dup", "[Pasted #1]")
    log.register("dup", "[Pasted #2]")
    assert log.take("dup") == "[Pasted #1]"  # FIFO
    assert log.take("dup") == "[Pasted #2]"
    assert log.take("dup") == "dup"  # exhausted


def test_clear_drops_all_pending_echoes() -> None:
    log = CompactEchoLog()
    log.register("a", "[A]")
    log.register("b", "[B]")
    assert log.key_count == 2
    log.clear()
    assert log.key_count == 0
    assert log.pending_count == 0
    assert log.order_length == 0
    assert log.take("a") == "a"  # nothing left


def test_registration_is_bounded_and_evicts_the_oldest() -> None:
    log = CompactEchoLog()
    overflow = MAX_PENDING_ECHOES + 10
    for i in range(overflow):
        log.register(f"prompt-{i}", f"[Pasted #{i}]")

    # Never grows past the cap.
    assert log.pending_count <= MAX_PENDING_ECHOES
    assert log.order_length <= MAX_PENDING_ECHOES
    # The oldest were evicted; the newest survives and echoes compact.
    assert log.take(f"prompt-{overflow - 1}") == f"[Pasted #{overflow - 1}]"
    # An evicted old prompt falls back to itself.
    assert log.take("prompt-0") == "prompt-0"


def test_taking_a_consumed_prompt_keeps_the_order_deque_exact() -> None:
    # Consuming an echo must also drop its insertion-order marker, so the cap
    # accounting stays exact and a later prompt isn't evicted on stale bookkeeping.
    log = CompactEchoLog(max_pending=3)
    log.register("x", "[X]")
    log.register("y", "[Y]")
    assert log.take("x") == "[X]"  # consumes x and its order marker
    assert log.order_length == 1  # only y's marker remains
    assert log.pending_count == 1


def test_custom_cap_is_respected() -> None:
    log = CompactEchoLog(max_pending=2)
    for i in range(5):
        log.register(f"p-{i}", f"[{i}]")
    assert log.pending_count <= 2
    assert log.take("p-4") == "[4]"  # newest kept
    assert log.take("p-0") == "p-0"  # oldest evicted
