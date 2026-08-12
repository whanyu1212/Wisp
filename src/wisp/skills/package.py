"""Package-owned Agent Skills shipped with Wisp."""

from __future__ import annotations

from pathlib import Path


def bundled_skills_root() -> Path:
    """Return the installed package directory containing Wisp-owned skills."""

    return Path(__file__).parent / "bundled"


__all__ = ["bundled_skills_root"]
