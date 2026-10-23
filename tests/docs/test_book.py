import re

import yaml

from tests.support.paths import DOCS_SRC_DIR

# ``{{#include path}}``, ``{{#include path:anchor}}`` or a line range such as ``path:2:10``.
_INCLUDE = re.compile(r"\{\{#include\s+([^}:\s]+)(?::([^}\s]*))?\s*\}\}")


def test_frontmatter_covers_every_published_source_and_unique_output() -> None:
    pages = sorted(DOCS_SRC_DIR.rglob("*.md"))
    assert pages
    routes: dict[str, str] = {}
    for path in pages:
        source = path.relative_to(DOCS_SRC_DIR).as_posix()
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n"), source
        metadata = yaml.safe_load(text.split("---", 2)[1])
        assert "source" not in metadata, source
        assert metadata["title"] and metadata["description"], source
        assert metadata["route"] == source.removesuffix(".md") + ".html", source
        assert isinstance(metadata["listed"], bool), source
        assert isinstance(metadata["searchable"], bool), source
        for route in {
            metadata["route"],
            metadata.get("legacySource", source).replace(".md", ".html"),
        }:
            assert route not in routes, (source, routes.get(route), route)
            routes[route] = source


def test_every_include_resolves_to_an_existing_file_and_anchor() -> None:
    # Resolve includes from their physical source location after relocation,
    # keeping runnable examples authoritative rather than copying snippets.
    includes = [
        (page, match)
        for page in sorted(DOCS_SRC_DIR.rglob("*.md"))
        for match in _INCLUDE.finditer(page.read_text(encoding="utf-8"))
    ]
    assert includes, "expected the crafting-agents chapters to include example code"

    problems: list[str] = []
    for page, match in includes:
        location = f"{page.relative_to(DOCS_SRC_DIR).as_posix()}: {match.group(0)}"
        target = (page.parent / match.group(1)).resolve()
        if not target.is_file():
            problems.append(f"{location} -> missing file {target}")
            continue
        anchor = match.group(2)
        if anchor and not re.fullmatch(r"[\d:]+", anchor):
            source = target.read_text(encoding="utf-8")
            if not (
                re.search(rf"ANCHOR:\s*{re.escape(anchor)}\b", source)
                and re.search(rf"ANCHOR_END:\s*{re.escape(anchor)}\b", source)
            ):
                problems.append(f"{location} -> no ANCHOR/ANCHOR_END '{anchor}' in {target}")

    assert not problems, "\n".join(problems)
