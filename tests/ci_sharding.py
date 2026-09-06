"""Opt-in CI partitioning; normal pytest collection is unchanged."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Generator
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("CI collection")
    group.addoption("--ci-shard-index", type=int, default=None)
    group.addoption("--ci-shard-count", type=int, default=None)
    group.addoption("--ci-node-manifest", type=Path, default=None)


def pytest_configure(config: pytest.Config) -> None:
    index = config.getoption("ci_shard_index")
    count = config.getoption("ci_shard_count")
    if (index is None) != (count is None) or (
        count is not None and (count < 1 or not 0 <= index < count)
    ):
        raise pytest.UsageError(
            "provide both --ci-shard-index and --ci-shard-count with 0 <= index < count"
        )


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> Generator[None]:
    # Run after marker/-k selection, including other collection hook wrappers.
    yield
    unsharded = [item.nodeid for item in items]
    index = config.getoption("ci_shard_index")
    count = config.getoption("ci_shard_count")
    if count is not None:
        selected, deselected = [], []
        for item in items:
            bucket = int.from_bytes(hashlib.sha256(item.nodeid.encode()).digest()) % count
            (selected if bucket == index else deselected).append(item)
        items[:] = selected
        config.hook.pytest_deselected(items=deselected)
    manifest = config.getoption("ci_node_manifest")
    if manifest is not None:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(
                {
                    "index": index,
                    "count": count,
                    "unsharded": unsharded,
                    "selected": [item.nodeid for item in items],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
