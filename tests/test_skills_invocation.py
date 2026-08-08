from __future__ import annotations

import hashlib
from pathlib import Path

import anyio
import pytest

from wisp.skills.invocation import expand_skill_invocation, parse_skill_invocation
from wisp.skills.models import SkillCatalog, SkillEntry
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError


def _catalog(tmp_path: Path) -> SkillCatalog:
    root = tmp_path / "demo"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo instructions\n---\nUse the narrow workflow.\n",
        encoding="utf-8",
    )
    return SkillCatalog(
        entries=(
            SkillEntry(
                name="demo",
                description="Demo instructions",
                source="user:wisp",
                root=root,
            ),
        )
    )


@pytest.mark.parametrize(
    ("content", "name", "expected_request"),
    [
        ("/skill:demo", "demo", ""),
        ("/skill:demo review this", "demo", "review this"),
        ("/skill:demo\nreview\nthis", "demo", "review\nthis"),
    ],
)
def test_parse_skill_invocation(content: str, name: str, expected_request: str) -> None:
    parsed = parse_skill_invocation(content)

    assert parsed is not None
    assert parsed.name == name
    assert parsed.request == expected_request
    assert parsed.original_content == content


@pytest.mark.parametrize(
    "content",
    [
        "use /skill:demo",
        " /skill:demo",
        "/skill:Demo",
        "/skill:demo/extra",
        "/skill:demo:extra",
        "/skill:demo!",
    ],
)
def test_parse_skill_invocation_leaves_non_directives_literal(content: str) -> None:
    assert parse_skill_invocation(content) is None


def test_expand_skill_invocation_retains_typed_evidence(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)

    async def scenario() -> tuple[str, object]:
        return await expand_skill_invocation(
            "/skill:demo review this",
            catalog=catalog,
            context=ToolContext(cwd=tmp_path),
        )

    expanded, evidence = anyio.run(scenario)

    assert evidence is not None
    assert evidence.name == "demo"
    assert evidence.original_content == "/skill:demo review this"
    assert evidence.request == "review this"
    assert evidence.content_sha256 == hashlib.sha256(b"Use the narrow workflow.\n").hexdigest()
    assert "[SKILL INSTRUCTIONS]\nUse the narrow workflow." in expanded
    assert expanded.endswith("[USER REQUEST]\nreview this")


def test_expand_skill_invocation_rejects_unknown_skill(tmp_path: Path) -> None:
    async def scenario() -> None:
        with pytest.raises(ToolError, match="Unknown skill 'missing'; available skills: none"):
            await expand_skill_invocation(
                "/skill:missing",
                catalog=SkillCatalog(),
                context=ToolContext(cwd=tmp_path),
            )

    anyio.run(scenario)
