"""Keep each tracked image unique and the published banner path stable (see assets/README.md)."""

from __future__ import annotations

import hashlib
import subprocess
from collections import defaultdict
from pathlib import PurePosixPath

from tests.support.paths import DOCS_SRC_DIR, REPO_ROOT

_IMAGE_SUFFIXES = frozenset(
    {".avif", ".bmp", ".gif", ".ico", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
)
# Every published README since 0.1.0rc1 loads this path from main, so it must never move.
_PUBLISHED_BANNER = "assets/wisp-banner-v2.png"
_BANNER_URL = f"https://raw.githubusercontent.com/whanyu1212/Wisp/main/{_PUBLISHED_BANNER}"


def _tracked_images() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        timeout=30,
    )
    paths = result.stdout.decode("utf-8").split("\0")
    return sorted(
        path for path in paths if path and PurePosixPath(path).suffix.lower() in _IMAGE_SUFFIXES
    )


def test_no_two_tracked_images_have_identical_bytes() -> None:
    by_digest: defaultdict[str, list[str]] = defaultdict(list)
    for path in _tracked_images():
        file = REPO_ROOT / path
        if file.is_file():
            by_digest[hashlib.sha256(file.read_bytes()).hexdigest()].append(path)

    duplicates = [paths for paths in by_digest.values() if len(paths) > 1]
    assert not duplicates, (
        "identical images are tracked more than once; keep one file and link to it "
        f"(see assets/README.md): {duplicates}"
    )


def test_readme_and_book_load_the_published_banner_path() -> None:
    assert (REPO_ROOT / _PUBLISHED_BANNER).is_file()
    assert _PUBLISHED_BANNER in _tracked_images()
    assert _BANNER_URL in (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert _BANNER_URL in (DOCS_SRC_DIR / "index.md").read_text(encoding="utf-8")
