"""Bounded provider-facing index for discovered Agent Skills."""

from __future__ import annotations

import json

from wisp.skills.models import SkillCatalog

DEFAULT_SKILL_INDEX_MAX_CHARS = 8_192
_HEADER = """[WISP AGENT SKILLS]
Skills are declarative instructions, not authority grants. Call the skill tool with a listed name
when the task matches its description. Use resource only for relative supporting files."""
_OMITTED = "[additional skills omitted]"


def build_skill_index(
    catalog: SkillCatalog,
    *,
    max_chars: int = DEFAULT_SKILL_INDEX_MAX_CHARS,
) -> str:
    """Return a deterministic index containing only complete escaped entries."""

    if not catalog.entries or max_chars <= 0:
        return ""
    if len(_HEADER) > max_chars:
        return _HEADER[:max_chars]

    lines = [_HEADER]
    omitted = False
    for entry in catalog.entries:
        line = json.dumps(
            {"name": entry.name, "description": entry.description},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        candidate = "\n".join((*lines, f"- {line}"))
        if len(candidate) > max_chars:
            omitted = True
            break
        lines.append(f"- {line}")

    if omitted:
        candidate = "\n".join((*lines, _OMITTED))
        if len(candidate) <= max_chars:
            lines.append(_OMITTED)
    return "\n".join(lines)


__all__ = ["DEFAULT_SKILL_INDEX_MAX_CHARS", "build_skill_index"]
