"""Async catalog loading at trusted application boundaries."""

from __future__ import annotations

from functools import partial
from pathlib import Path

import anyio

from wisp.skills.discovery import discover_skills
from wisp.skills.models import SkillCatalog


async def discover_skill_catalog(
    *,
    project_root: Path | None,
    trusted: bool,
    protected_paths: tuple[str, ...],
) -> SkillCatalog:
    """Discover user skills and trust-eligible project skills off the event loop."""

    return await anyio.to_thread.run_sync(
        partial(
            discover_skills,
            home_dir=Path.home(),
            project_root=project_root if trusted else None,
            protected_paths=protected_paths,
        ),
        abandon_on_cancel=True,
    )


__all__ = ["discover_skill_catalog"]
