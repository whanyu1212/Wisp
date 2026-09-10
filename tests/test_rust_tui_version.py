from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.version import Version

from wisp import __version__


def test_rust_tui_package_version_matches_python_distribution() -> None:
    root = Path(__file__).resolve().parents[1]
    cargo_manifest = tomllib.loads((root / "rust/wisp-tui/Cargo.toml").read_text(encoding="utf-8"))

    version = Version(__version__)
    expected = version.base_version
    if version.pre is not None:
        label, number = version.pre
        cargo_label = {"a": "alpha", "b": "beta", "rc": "rc"}[label]
        expected += f"-{cargo_label}.{number}"

    # Only spelling differs; epochs, post/dev releases, and local labels are
    # not silently dropped from the exact-lockstep contract.
    assert Version(expected) == version
    assert cargo_manifest["package"]["version"] == expected
    cargo_lock = tomllib.loads((root / "Cargo.lock").read_text(encoding="utf-8"))
    locked = next(package for package in cargo_lock["package"] if package["name"] == "wisp-tui")
    assert locked["version"] == expected
