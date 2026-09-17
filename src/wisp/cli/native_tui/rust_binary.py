"""Portable discovery of the Rust frontend owned by the active Wisp installation."""

from importlib import metadata
from pathlib import Path


def installed_rust_tui_binary() -> Path | None:
    """Find the native script in distribution metadata without searching PATH.

    Returns:
        Path | None: The declared script path, or None for a pure/source installation.
            A declared but missing or damaged binary is returned for launch validation;
            it must not silently change the selected frontend.
    """
    try:
        distribution = metadata.distribution("wisp-ai")
    except metadata.PackageNotFoundError:
        return None
    binary = next((entry for entry in distribution.files or () if entry.name == "wisp-tui"), None)
    return Path(binary.locate()) if binary is not None else None
