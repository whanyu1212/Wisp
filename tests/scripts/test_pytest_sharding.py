"""Tests for deterministic pytest sharding in ``tests/conftest.py``."""

from __future__ import annotations

import subprocess
import sys

import pytest

from tests.support.paths import REPO_ROOT
from tests.support.pytest_sharding import parse_shard_spec, partition_shard


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1/1", (1, 1)),
        ("1/3", (1, 3)),
        ("3/3", (3, 3)),
        ("10/12", (10, 12)),
    ],
)
def test_parses_valid_shard_specs(raw: str, expected: tuple[int, int]) -> None:
    assert parse_shard_spec(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "1",
        "1/2/3",
        "0/2",
        "3/2",
        "1/0",
        "-1/2",
        "+1/2",
        " 1/2",
        "1/2 ",
        "a/b",
        "١/2",
    ],
)
def test_rejects_invalid_shard_specs(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_shard_spec(raw)


@pytest.mark.parametrize("shard_count", [1, 2, 3, 5])
def test_partition_is_disjoint_and_exhaustive(shard_count: int) -> None:
    items = [f"item-{i}" for i in range(17)]
    seen: list[str] = []

    for shard_index in range(1, shard_count + 1):
        selected, deselected = partition_shard(items, shard_index, shard_count)
        assert len(selected) + len(deselected) == len(items)
        assert set(selected).isdisjoint(deselected)
        assert selected == [item for item in items if item in selected]
        seen.extend(selected)

    assert len(seen) == len(items)
    assert set(seen) == set(items)


def _collect_node_ids(*args: str) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if "::" in line]


def test_cli_shards_filtered_collection() -> None:
    baseline = _collect_node_ids("tests/sessions/test_replay.py", "-m", "not benchmark")
    shards = [
        _collect_node_ids(
            "tests/sessions/test_replay.py", "-m", "not benchmark", "--shard", f"{i}/3"
        )
        for i in (1, 2, 3)
    ]

    assert all(shard for shard in shards)
    combined = [node_id for shard in shards for node_id in shard]
    assert len(combined) == len(baseline)
    assert set(combined) == set(baseline)


def test_cli_runs_shard_with_xdist() -> None:
    baseline = set(_collect_node_ids("tests/test_retry.py"))
    executed: list[str] = []
    expected_counts = {"1/2": (len(baseline) + 1) // 2, "2/2": len(baseline) // 2}

    for spec, count in expected_counts.items():
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_retry.py",
                "-n",
                "2",
                "--shard",
                spec,
                "-v",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"{count} passed" in result.stdout
        for line in result.stdout.splitlines():
            if " PASSED " in line and "tests/test_retry.py::" in line:
                executed.append(line.split(" PASSED ")[1].strip())

    assert len(executed) == len(baseline)
    assert set(executed) == baseline


def test_cli_rejects_out_of_range_shard() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_retry.py", "--shard", "4/3"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 4
    assert "Invalid shard specification" in result.stderr
