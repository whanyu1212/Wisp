from __future__ import annotations

import tomllib
from pathlib import Path

from wisp import __version__


def test_rust_tui_package_version_matches_python_distribution() -> None:
    root = Path(__file__).resolve().parents[1]
    cargo_manifest = tomllib.loads((root / "rust/wisp-tui/Cargo.toml").read_text(encoding="utf-8"))

    assert cargo_manifest["package"]["version"] == __version__
