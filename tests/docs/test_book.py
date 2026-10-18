import re

from tests.support.paths import BOOK_SRC_DIR

_SUMMARY = BOOK_SRC_DIR / "SUMMARY.md"

# ``{{#include path}}``, ``{{#include path:anchor}}`` or a line range such as ``path:2:10``.
_INCLUDE = re.compile(r"\{\{#include\s+([^}:\s]+)(?::([^}\s]*))?\s*\}\}")


def test_summary_lists_every_published_page_once() -> None:
    summary_targets = re.findall(r"\[[^]]+\]\(([^)]+\.md)\)", _SUMMARY.read_text(encoding="utf-8"))
    published_pages = {
        path.relative_to(BOOK_SRC_DIR).as_posix()
        for path in BOOK_SRC_DIR.rglob("*.md")
        if path != _SUMMARY
    }

    assert len(summary_targets) == len(set(summary_targets))
    assert set(summary_targets) == published_pages


def test_every_include_resolves_to_an_existing_file_and_anchor() -> None:
    # mdBook only logs an unreadable include and still exits 0, publishing the raw
    # directive, so a moved page or example file would otherwise break silently.
    includes = [
        (page, match)
        for page in sorted(BOOK_SRC_DIR.rglob("*.md"))
        for match in _INCLUDE.finditer(page.read_text(encoding="utf-8"))
    ]
    assert includes, "expected the crafting-agents chapters to include example code"

    problems: list[str] = []
    for page, match in includes:
        location = f"{page.relative_to(BOOK_SRC_DIR).as_posix()}: {match.group(0)}"
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
