"""Check corpus completeness and reproducibility without invoking nightly tooling."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prepare_fuzz_corpus", ROOT / "scripts/prepare_fuzz_corpus.py"
)
assert SPEC is not None and SPEC.loader is not None
CORPUS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CORPUS)


def test_corpus_is_complete_reproducible_and_preserves_raw_seeds(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    CORPUS.prepare_corpus(first)
    CORPUS.prepare_corpus(second)
    first_files = {p.relative_to(first): p.read_bytes() for p in first.rglob("*") if p.is_file()}
    second_files = {p.relative_to(second): p.read_bytes() for p in second.rglob("*") if p.is_file()}
    assert first_files == second_files
    for family, schema in (
        ("client_wire", "commands.schema.json"),
        ("server_wire", "events.schema.json"),
    ):
        fixtures = json.loads((ROOT / "schemas/live-rpc/v6" / schema).read_text())[
            "x-wisp-conformance-fixtures"
        ]
        generated = list((first / family).glob("fixture-*.json"))
        assert len(generated) == len(fixtures)
        assert {json.loads(p.read_bytes())["type"] for p in generated} == set(fixtures)
        seeds = list((ROOT / "fuzz/seeds" / family).glob("*/*"))
        assert len(list((first / family).iterdir())) == len(fixtures) + len(seeds)
        for seed in seeds:
            assert (
                first / family / f"{seed.parent.name}-{seed.name}"
            ).read_bytes() == seed.read_bytes()
    with pytest.raises(FileExistsError):
        CORPUS.prepare_corpus(first)
