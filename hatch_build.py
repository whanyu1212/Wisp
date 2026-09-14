"""Candidate platform-wheel hook for the optional Rust TUI."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_WHEEL_TAG_ENV = "WISP_RUST_TUI_WHEEL_TAG"


class CustomBuildHook(BuildHookInterface):
    """Build and package the lockstep Rust TUI candidate binary."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        """Build the Rust frontend and add it to the wheel scripts directory.

        Args:
            version: Hatch wheel build version.
            build_data: Mutable Hatch wheel metadata and inclusion settings.

        Raises:
            RuntimeError: If the explicit candidate platform tag is absent.
            subprocess.CalledProcessError: If the locked release build fails.
        """

        if version != "standard":
            return
        tag = os.environ.get(_WHEEL_TAG_ENV)
        if not tag:
            raise RuntimeError(f"{_WHEEL_TAG_ENV} is required for Rust TUI candidate wheels")

        subprocess.run(
            [
                "cargo",
                "rustc",
                "--release",
                "--locked",
                "--package",
                "wisp-tui",
                "--bin",
                "wisp-tui",
                "--",
                "-C",
                "strip=symbols",
            ],
            cwd=self.root,
            check=True,
        )
        target_dir = Path(os.environ.get("CARGO_TARGET_DIR", Path(self.root, "target")))
        binary = target_dir / "release" / "wisp-tui"
        if not binary.is_file():
            raise RuntimeError(f"Rust TUI build did not produce {binary}")

        build_data["pure_python"] = False
        build_data["tag"] = tag
        build_data["shared_scripts"][str(binary)] = "wisp-tui"
