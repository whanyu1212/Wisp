"""Deterministic round-robin test sharding helpers for CI matrix jobs."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

_ItemT = TypeVar("_ItemT")


def parse_shard_spec(raw: str) -> tuple[int, int]:
    """Parse a 1-based ``INDEX/TOTAL`` shard specification.

    Args:
        raw: Raw CLI argument string in ``INDEX/TOTAL`` form.

    Returns:
        A ``(shard_index, shard_count)`` pair where ``1 <= shard_index <= shard_count``.

    Raises:
        ValueError: If ``raw`` is malformed or specifies an out-of-range shard.
    """

    parts = raw.split("/")
    if len(parts) != 2:
        raise ValueError(f"Invalid shard specification {raw!r}; expected INDEX/TOTAL.")
    index_text, count_text = parts
    if not (
        index_text
        and count_text
        and index_text.isascii()
        and count_text.isascii()
        and index_text.isdigit()
        and count_text.isdigit()
    ):
        raise ValueError(f"Invalid shard specification {raw!r}; expected positive integers.")
    shard_index = int(index_text)
    shard_count = int(count_text)
    if shard_count < 1 or not (1 <= shard_index <= shard_count):
        raise ValueError(
            f"Invalid shard specification {raw!r}; require 1 <= INDEX <= TOTAL and TOTAL >= 1."
        )
    return shard_index, shard_count


def partition_shard(
    items: Sequence[_ItemT],
    shard_index: int,
    shard_count: int,
) -> tuple[list[_ItemT], list[_ItemT]]:
    """Split ``items`` into selected and deselected lists for one shard.

    Uses round-robin assignment (``position % shard_count == shard_index - 1``) so
    multi-test files are balanced across shards while keeping relative order intact.

    Args:
        items: Collected test items in execution order.
        shard_index: 1-based shard index to select.
        shard_count: Total number of shards (>= 1).

    Returns:
        Tuple of ``(selected, deselected)`` item lists.

    Raises:
        ValueError: If ``shard_index`` or ``shard_count`` is out of range.
    """

    if shard_count < 1 or not (1 <= shard_index <= shard_count):
        raise ValueError(
            f"Invalid shard arguments ({shard_index}, {shard_count}); "
            "require 1 <= shard_index <= shard_count."
        )
    target = shard_index - 1
    selected: list[_ItemT] = []
    deselected: list[_ItemT] = []
    for position, item in enumerate(items):
        if position % shard_count == target:
            selected.append(item)
        else:
            deselected.append(item)
    return selected, deselected
