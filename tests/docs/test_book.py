import re

from tests.support.paths import REPO_ROOT

_REPOSITORY_ROOT = REPO_ROOT
_SITE_ROOT = _REPOSITORY_ROOT / "site"
_SUMMARY = _SITE_ROOT / "SUMMARY.md"


def test_summary_lists_every_published_page_once() -> None:
    summary_targets = re.findall(r"\[[^]]+\]\(([^)]+\.md)\)", _SUMMARY.read_text(encoding="utf-8"))
    published_pages = {
        path.relative_to(_SITE_ROOT).as_posix()
        for path in _SITE_ROOT.rglob("*.md")
        if path != _SUMMARY
    }

    assert len(summary_targets) == len(set(summary_targets))
    assert set(summary_targets) == published_pages
