"""Candidate platform-wheel hook for Wisp's optional native components."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_WHEEL_TAG_ENV = "WISP_RUST_TUI_WHEEL_TAG"
_EXTENSION_DESTINATION = "wisp/_native.abi3.so"


class CustomBuildHook(BuildHookInterface):
    """Build and package the lockstep Rust TUI and Python extension."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        """Build the Rust components and add them to the platform wheel.

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
        subprocess.run(
            [
                "cargo",
                "rustc",
                "--release",
                "--locked",
                "--package",
                "wisp-python",
                "--lib",
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
        extension_candidates = [
            target_dir / "release" / "lib_native.so",
            target_dir / "release" / "lib_native.dylib",
        ]
        extension = next((path for path in extension_candidates if path.is_file()), None)
        if extension is None:
            raise RuntimeError(
                "Rust Python build did not produce "
                + " or ".join(str(path) for path in extension_candidates)
            )

        build_data["pure_python"] = False
        build_data["tag"] = tag
        build_data.setdefault("shared_scripts", {})[str(binary)] = "wisp-tui"
        build_data.setdefault("force_include", {})[str(extension)] = _EXTENSION_DESTINATION
