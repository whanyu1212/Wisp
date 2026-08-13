from __future__ import annotations

import pytest

from wisp.tui.transcript_window import TUI_TRANSCRIPT_WINDOW_SIZE, TranscriptWindow


def test_default_window_retains_more_entries_than_it_exposes() -> None:
    window = TranscriptWindow[int]()
    retained_count = TUI_TRANSCRIPT_WINDOW_SIZE + 1

    window.replace(range(retained_count))

    assert window.retained_count == retained_count
    assert len(window.visible) == TUI_TRANSCRIPT_WINDOW_SIZE
    assert window.visible[0] == 1
    assert window.visible[-1] == retained_count - 1


def test_window_keeps_latest_entries_bounded() -> None:
    window = TranscriptWindow[int](capacity=3, shift=1)

    window.replace(range(6))

    assert window.visible == (3, 4, 5)
    assert window.is_at_latest
    assert not window.is_at_oldest


def test_window_shifts_through_retained_entries() -> None:
    window = TranscriptWindow[int](capacity=3, shift=1)
    window.replace(range(6))

    assert window.shift_older()
    assert window.visible == (2, 3, 4)
    assert window.shift_older()
    assert window.visible == (1, 2, 3)
    assert window.shift_newer()
    assert window.visible == (2, 3, 4)


def test_prepend_preserves_visible_entries() -> None:
    window = TranscriptWindow[int](capacity=3, shift=1)
    window.replace((3, 4, 5))

    window.prepend((0, 1, 2))

    assert window.visible == (3, 4, 5)
    assert window.shift_older()
    assert window.visible == (2, 3, 4)


def test_prepend_clamps_an_underfilled_window() -> None:
    window = TranscriptWindow[int](capacity=3, shift=1)

    window.prepend((0, 1, 2))

    assert window.visible == (0, 1, 2)
    assert window.is_at_oldest
    assert window.is_at_latest


def test_append_only_moves_window_for_tail_following() -> None:
    window = TranscriptWindow[int](capacity=3, shift=1)
    window.replace(range(6))
    window.shift_older()

    window.append((6,), follow_tail=False)

    assert window.visible == (2, 3, 4)
    window.append((7,), follow_tail=True)
    assert window.visible == (5, 6, 7)


def test_prepend_evicts_newest_entries_when_retention_is_full() -> None:
    window = TranscriptWindow[int](capacity=3, shift=1, retained_capacity=6)
    window.replace(range(6))
    window.shift_older()

    evicted = window.prepend((-3, -2, -1))

    assert evicted == (3, 4, 5)
    assert window.entries == (-3, -2, -1, 0, 1, 2)
    assert window.visible == (0, 1, 2)
    assert not window.latest_is_retained


def test_append_evicts_oldest_entries_and_keeps_latest_retained() -> None:
    window = TranscriptWindow[int](capacity=3, shift=1, retained_capacity=6)
    window.replace(range(6))

    evicted = window.append((6, 7), follow_tail=True)

    assert evicted == (0, 1)
    assert window.entries == (2, 3, 4, 5, 6, 7)
    assert window.visible == (5, 6, 7)
    assert window.latest_is_retained


@pytest.mark.parametrize(
    ("capacity", "shift"),
    [(0, 1), (2, 0), (2, 3)],
)
def test_window_rejects_invalid_limits(capacity: int, shift: int) -> None:
    with pytest.raises(ValueError):
        TranscriptWindow[int](capacity=capacity, shift=shift)


def test_window_rejects_retention_smaller_than_visible_capacity() -> None:
    with pytest.raises(ValueError, match="retained_capacity"):
        TranscriptWindow[int](capacity=2, shift=1, retained_capacity=1)
