from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.verify_ci_shards import verify_shards

pytest_plugins = ["pytester"]


def test_shards_partition_filtered_collection_and_preserve_default(
    pytester: pytest.Pytester,
) -> None:
    plugin = Path(__file__).with_name("ci_sharding.py").read_text()
    pytester.makeconftest(plugin)
    pytester.makeini("[pytest]\nmarkers = excluded: filtered out")
    pytester.makepyfile("""
import pytest
@pytest.mark.parametrize("value", range(24))
def test_selected(value): pass
@pytest.mark.excluded
def test_excluded(): pass
def test_other(): pass
""")
    manifests = []
    for index in (None, 0, 1):
        path = pytester.path / f"nodes-{index}.json"
        args = ["-m", "not excluded", "-k", "selected", f"--ci-node-manifest={path}"]
        if index is not None:
            args += [f"--ci-shard-index={index}", "--ci-shard-count=2"]
            manifests.append(path)
        result = pytester.runpytest_subprocess(*args)
        assert result.ret == 0
    baseline = json.loads((pytester.path / "nodes-None.json").read_text())["selected"]
    assert len(baseline) == 24
    for path in manifests:
        entry = json.loads(path.read_text())
        assert entry["unsharded"] == baseline
        assert 0 < len(entry["selected"]) < 24
    verify_shards(manifests)
    # A repeat produces exactly the same assignment and collection order.
    previous = manifests[0].read_text()
    pytester.runpytest_subprocess(
        "--collect-only",
        "-m",
        "not excluded",
        "-k",
        "selected",
        "--ci-shard-index=0",
        "--ci-shard-count=2",
        f"--ci-node-manifest={manifests[0]}",
    ).assert_outcomes()
    assert manifests[0].read_text() == previous


@pytest.mark.parametrize(
    "args",
    [
        ["--ci-shard-index=0"],
        ["--ci-shard-count=2"],
        ["--ci-shard-index=-1", "--ci-shard-count=2"],
        ["--ci-shard-index=2", "--ci-shard-count=2"],
        ["--ci-shard-index=0", "--ci-shard-count=0"],
        ["--ci-shard-index=x", "--ci-shard-count=2"],
    ],
)
def test_invalid_shard_options_fail_collection(pytester: pytest.Pytester, args: list[str]) -> None:
    pytester.makeconftest(Path(__file__).with_name("ci_sharding.py").read_text())
    pytester.makepyfile("def test_example(): pass")
    assert pytester.runpytest_subprocess(*args).ret == pytest.ExitCode.USAGE_ERROR


@pytest.mark.parametrize("fault", ["missing", "duplicate", "omitted", "extra", "disagreement"])
def test_collection_audit_rejects_incomplete_or_overlapping_shards(
    tmp_path: Path, fault: str
) -> None:
    entries = [
        {"index": 0, "count": 2, "unsharded": ["a", "b"], "selected": ["a"]},
        {"index": 1, "count": 2, "unsharded": ["a", "b"], "selected": ["b"]},
    ]
    if fault == "missing":
        entries.pop()
    elif fault == "duplicate":
        entries[1]["selected"] = ["a", "b"]
    elif fault == "omitted":
        entries[1]["selected"] = []
    elif fault == "extra":
        entries[1]["selected"] = ["b", "c"]
    else:
        entries[1]["unsharded"] = ["b"]
    paths = []
    for index, entry in enumerate(entries):
        path = tmp_path / f"{index}.json"
        path.write_text(json.dumps(entry))
        paths.append(path)
    with pytest.raises(ValueError):
        verify_shards(paths)
