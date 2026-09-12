"""Prepare disposable fuzz corpora from canonical fixtures and regression seeds."""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def prepare_corpus(output: Path, *, root: Path = ROOT) -> None:
    """Write deterministic initial corpora into a new directory.

    Args:
        output (Path): New destination; existing paths are rejected to preserve runs.
        root (Path): Repository containing canonical schemas and committed seeds.

    Raises:
        FileExistsError: If the destination already exists.
        ValueError: If a schema has no fixtures or a fixture name is not a filename.
    """
    output.mkdir(parents=True, exist_ok=False)
    for family, schema in (
        ("client_wire", "commands.schema.json"),
        ("server_wire", "events.schema.json"),
    ):
        destination = output / family
        destination.mkdir()
        document = json.loads((root / "schemas/live-rpc/v6" / schema).read_text())
        fixtures = document["x-wisp-conformance-fixtures"]
        if not fixtures:
            raise ValueError(f"No canonical fixtures in {schema}")
        for name, fixture in sorted(fixtures.items()):
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                raise ValueError(f"Invalid fixture name: {name!r}")
            encoded = json.dumps(fixture, sort_keys=True, separators=(",", ":"))
            (destination / f"fixture-{name}.json").write_bytes(encoded.encode() + b"\n")
        for outcome in ("valid", "invalid"):
            for seed in sorted((root / "fuzz/seeds" / family / outcome).iterdir()):
                (destination / f"{outcome}-{seed.name}").write_bytes(seed.read_bytes())


def main() -> None:
    """Parse the destination and prepare the corpus."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new directory for generated corpora")
    args = parser.parse_args()
    prepare_corpus(args.output)


if __name__ == "__main__":
    main()
