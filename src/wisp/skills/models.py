"""Immutable values produced by Agent Skills discovery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

type SkillSource = Literal["project:wisp", "project:agents", "user:wisp", "user:agents"]
type SkillDiagnosticSeverity = Literal["warning", "error"]
type SkillDiagnosticCode = Literal[
    "catalog-limit",
    "entry-symlink",
    "file-unreadable",
    "invalid-frontmatter",
    "invalid-metadata",
    "invalid-yaml",
    "path-escape",
    "protected-path",
    "root-entry-limit",
    "root-metadata-limit",
    "root-not-directory",
    "root-symlink",
    "root-unreadable",
    "shadowed",
    "unsupported-field",
]


class SkillInvocationEvidence(BaseModel):
    """Typed evidence retained with one explicitly expanded user message."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    original_content: str
    request: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    instructions_truncated: bool = False


@dataclass(frozen=True, slots=True)
class SkillEntry:
    """Validated metadata and the internal root for one discoverable skill."""

    name: str
    description: str
    source: SkillSource
    root: Path
    license: str | None = None
    compatibility: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()
    allowed_tools: str | None = None


@dataclass(frozen=True, slots=True)
class SkillDiagnostic:
    """One isolated discovery problem that did not abort the whole catalog."""

    code: SkillDiagnosticCode
    severity: SkillDiagnosticSeverity
    message: str
    source: SkillSource
    path: Path | None = None


@dataclass(frozen=True, slots=True)
class SkillCatalog:
    """A deterministic immutable snapshot of available skill metadata."""

    entries: tuple[SkillEntry, ...] = ()
    diagnostics: tuple[SkillDiagnostic, ...] = ()

    def get(self, name: str) -> SkillEntry | None:
        """Return one skill by its validated name."""

        return next((entry for entry in self.entries if entry.name == name), None)

    def names(self) -> tuple[str, ...]:
        """Return skill names in deterministic catalog order."""

        return tuple(entry.name for entry in self.entries)
