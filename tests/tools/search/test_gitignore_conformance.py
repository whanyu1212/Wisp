"""Cross-language conformance for gitignore matching.

The Python walk decides what is ignored with pathspec; the native walk uses the Rust
port in `rust/wisp-search/src/gitignore.rs`. The shared fixture records pathspec's
verdict for every pair of ignore file and path below, including undecodable bytes,
and `rust/wisp-search/tests/gitignore_conformance.rs` checks the port reaches the
same ones. Regenerate after changing ignore matching or upgrading pathspec:

    UPDATE_GITIGNORE_FIXTURE=1 uv run pytest tests/tools/search/test_gitignore_conformance.py
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path

import pytest
from pathspec import GitIgnoreSpec

from tests.support.paths import FIXTURES_DIR
from wisp.tools.search.tools import (
    _compile_ignore_lines,
    _IgnoreSpec,
    _may_reinclude_descendant,
)

_FIXTURE_PATH = FIXTURES_DIR / "gitignore_conformance.json"
# Not WISP_-prefixed: the test environment fixture clears every WISP_* variable.
_UPDATE_ENVIRONMENT_VARIABLE = "UPDATE_GITIGNORE_FIXTURE"
_BASE = Path("/base")
_SEED = 11
_RANDOM_CASES = 160

_PATTERNS = (
    "",
    "#comment",
    "\\#hash",
    "\\!bang",
    "/",
    "//",
    "*",
    "**",
    "**/",
    "*/",
    "/*",
    "/**",
    "**/**",
    "foo",
    "/foo",
    "foo/",
    "/foo/",
    "foo/**",
    "foo/**/",
    "**/foo",
    "**/foo/**",
    "a/**/b",
    "a/**/**/b",
    "a/*/b",
    "a/b",
    "a/b/",
    "*.py",
    "a/*.py",
    "?.py",
    "x.???",
    "a?b",
    "a*b",
    "a\\*b",
    "a\\?b",
    "[ab]",
    "[!a]",
    "[^a]",
    "[a-c]",
    "[a-a]",
    "[z-a]",
    "[]]",
    "[]-]",
    "[!]a-]",
    "[[:alpha:]]",
    "[a&&b]",
    "[a--b]",
    "[a~~b]",
    "[a||b]",
    "[\\]",
    "[\\]]",
    "[abc",
    "x[",
    "[/]",
    "a[!x]b",
    "[é]",
    "[à-ÿ]",
    "[!é]",
    "é",
    "a\udc80b",
    "[\udc80-\udcff]",
    "[!\udc80]",
    "?",
    "foo\\",
    "trail\\ ",
    "trailing   ",
    "  leading",
    "a b",
    "!foo",
    "!",
    "!/",
    "!*.py",
    "\\",
    "\\\\",
    "a\\\\b",
    "-",
    " ",
    "x\x85",
    "build/",
    "doc/**/*.txt",
)

_CONTENTS = (
    "*.log\n!keep.log\n",
    "!build/keep.txt\nbuild/\n",
    "build/\n!build/keep.txt\n",
    "foo/**\n!foo/bar\n",
    "foo/**\n!/foo/bar\n",
    "/foo/**\n!foo/x/y\n",
    "a/**\n!a/b/c\n",
    "a/**\n!b\n",
    "**/foo/**\n!foo/bar\n",
    "foo/\n!foo/bar\n",
    "x/**\n!x/y/z\n!/x/w\n",
    "**/a/**\n!a/b/c\n",
    "*\n!*/\n!*.py\n",
    "a\r\nb\rc\x0bd\x0ce\x1cf\x1dg\x1eh\x85i j k\n",
    "foo\\ \nbar\\\n",
    "#x\n\nfoo\n#y\n!foo\n",
)

_PATH_COMPONENTS = (
    "a",
    "b",
    "c",
    "foo",
    "bar",
    "x.py",
    "keep.log",
    "é",
    "a b",
    "[a]",
    "-",
    "\\",
    "a\\b",
    "#a",
    "!a",
    "a\udc80b",
    "\udc80",
    "\udcc3",
    "a\n",
    " ",
    "z",
)

_PATHS = (
    "foo",
    "foo/bar",
    "foo/x/y",
    "a/foo",
    "a/foo/b",
    "bar",
    "x.py",
    "a/x.py",
    "a/b/x.py",
    "a/b",
    "a/b/c",
    "a/x/b",
    "a/x/y/b",
    "b/a",
    "build",
    "build/keep.txt",
    "build/x/y",
    "keep.log",
    "a/keep.log",
    "doc/a.txt",
    "doc/x/a.txt",
    "x.abc",
    "#hash",
    "!bang",
    "a b",
    "trail ",
    "trailing",
    "  leading",
    "\\",
    "a\\b",
    "a*b",
    "a?b",
    "aéb",
    "a\udc80b",
    "a\udcc3b",
    "\udc80",
    "\udcff",
    "é",
    "ÿ",
    "[x]",
    "]",
    "-",
    "a\n",
    "\n",
    "foo\n",
    " ",
    "x\x85",
    "Z",
    "z",
)


def _hex(text: str) -> str:
    return text.encode("utf-8", errors="surrogateescape").hex()


def _verdict(spec: GitIgnoreSpec, path: str) -> str:
    """Encode one ``check_file`` result as a character."""

    result = spec.check_file(path)
    if result.include is None:
        return "-"
    assert result.index is not None and result.index < 26
    return chr((ord("A") if result.include else ord("a")) + result.index)


def _random_contents() -> list[str]:
    generator = random.Random(_SEED)
    tokens = ("a", "b", "/", "*", "**", "?", "[", "]", "!", "^", "-", "\\", "é", "\udc80", " ")
    contents = []
    for _ in range(_RANDOM_CASES):
        lines = [
            "".join(generator.choice(tokens) for _ in range(generator.randint(1, 6)))
            for _ in range(generator.choice((1, 1, 1, 2, 3)))
        ]
        contents.append("".join(f"{line}\n" for line in lines))
    return contents


def _random_paths() -> list[str]:
    generator = random.Random(_SEED)
    return [
        "/".join(generator.choice(_PATH_COMPONENTS) for _ in range(generator.randint(1, 3)))
        for _ in range(30)
    ]


def _fixture_text() -> str:
    paths = list(dict.fromkeys([*_PATHS, *_random_paths()]))
    contents = [f"{pattern}\n" for pattern in _PATTERNS] + [*_CONTENTS, *_random_contents()]
    cases = []
    for content in contents:
        spec = _compile_ignore_lines(content.splitlines(keepends=True))
        ignore_specs = (_IgnoreSpec(_BASE, spec, 1),)
        cases.append(
            {
                "content": _hex(content),
                "file": "".join(_verdict(spec, path) for path in paths),
                "directory": "".join(_verdict(spec, f"{path}/") for path in paths),
                "reinclude": "".join(
                    "1" if _may_reinclude_descendant(_BASE / path, ignore_specs) else "0"
                    for path in paths
                ),
            }
        )
    fixture = {"version": 1, "paths": [_hex(path) for path in paths], "cases": cases}
    return json.dumps(fixture, indent=1) + "\n"


@pytest.mark.skipif(os.name == "nt", reason="the fixture uses POSIX path separators")
# Python warns that "[[" and "&&" in a class may change meaning; pathspec passes them through.
@pytest.mark.filterwarnings("ignore::FutureWarning")
def test_gitignore_fixture_matches_pathspec() -> None:
    expected = _fixture_text()
    if os.environ.get(_UPDATE_ENVIRONMENT_VARIABLE) == "1":
        _FIXTURE_PATH.write_text(expected, encoding="utf-8")
    assert _FIXTURE_PATH.read_text(encoding="utf-8") == expected, (
        f"stale {_FIXTURE_PATH.name}; regenerate with {_UPDATE_ENVIRONMENT_VARIABLE}=1"
    )
