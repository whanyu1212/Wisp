"""Fail CI if shard collections overlap or omit selected tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def verify_shards(paths: list[Path]) -> None:
    manifests = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    if not manifests:
        raise ValueError("no shard manifests")
    count = manifests[0]["count"]
    if not isinstance(count, int) or count < 1 or len(manifests) != count:
        raise ValueError("missing shard manifests")
    if {entry["index"] for entry in manifests} != set(range(count)):
        raise ValueError("missing or duplicate shard indices")
    expected = set(manifests[0]["unsharded"])
    if not expected:
        raise ValueError("empty unsharded collection")
    selected: set[str] = set()
    for entry in manifests:
        if entry["count"] != count or set(entry["unsharded"]) != expected:
            raise ValueError("shards disagree on unsharded collection")
        nodes = entry["selected"]
        if len(nodes) != len(set(nodes)) or selected.intersection(nodes):
            raise ValueError("duplicate test nodes across shards")
        selected.update(nodes)
    if selected != expected:
        raise ValueError("shards omit or add test nodes")
    print(f"Verified {len(selected)} test nodes across {count} disjoint shards")


if __name__ == "__main__":
    verify_shards([Path(argument) for argument in sys.argv[1:]])
