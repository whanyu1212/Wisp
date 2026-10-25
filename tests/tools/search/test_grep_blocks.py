"""Python grep searches a block of lines once before searching its lines one by one.

The block search must never change a result, only skip blocks with no match.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from tests.tools.support import run_tool
from wisp.tools.builtin import GrepTool
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolResult
from wisp.tools.search import tools as search_tools_module

_LINES = ("foo", "Foo bar", "a1c", "abc", "x y", "é", "b", "bbc", "", "zz", "ab cd", "1234", "fooz")
_SEPARATORS = ("\n", "\n", "\r\n", "\r", "\x0b", " ")
_PATTERNS = (
    ("foo", False, False),
    ("foo", True, False),
    ("Foo", False, True),
    ("ab cd", True, True),
    (r"\d+", False, False),
    ("a.c", False, False),
    ("(?:ab|cd)", False, False),
    ("(?P<x>b)c", False, False),
    ("(?i)foo", False, False),
    ("[^a]b", False, False),
    (r"x\sy", False, False),
    (r"\w+z", False, False),
    ("b{2}", False, False),
    ("é", True, False),
    ("^foo", False, False),
    ("foo$", False, False),
    (r"\bfoo", False, False),
    ("(?=b)b", False, False),
    (r"\bb\b", False, False),
    (r"\Bc", False, False),
    ("^$", False, False),
    ("^", False, False),
    ("c$", False, False),
    ("^a.c$", False, False),
    (r"(?m)^\w+$", False, False),
    ("absent", False, False),
    ("^[^x]*+$", False, False),
    ("[^x]++c", False, False),
)


@pytest.mark.parametrize(
    "pattern",
    [
        "foo",
        r"a\d+b",
        "(?:a|b)",
        "(?P<name>x)y",
        "(?<name>x)",
        "(?i)foo",
        "(?is:x)",
        '[^"]+',
        r"[\]^$]",
        r"\\b",
        "(?P=name)",
        r"\p{L}+",
        r"\N{LATIN SMALL LETTER E WITH ACUTE}",
        r"\x{41}{2}",
        "a{2,3}",
        "^foo",
        "foo$",
        "^$",
        r"\bfoo\b",
        r"\Bo",
        r"\mfoo",
        "(?m)^x",
    ],
)
def test_block_search_accepts_local_patterns(pattern: str) -> None:
    assert search_tools_module._can_search_blocks(pattern)


@pytest.mark.parametrize(
    "pattern",
    [
        r"\Afoo",
        r"foo\Z",
        r"foo\z",
        r"\Gfoo",
        "(?-m)^x",
        "(?i-m:x)",
        "(?=x)",
        "(?!x)",
        "(?<=x)y",
        "(?<!x)y",
        "(?>x)",
        "(?(1)a|b)",
        "(*SKIP)",
        "(*i)",
        "^[^x]*+$",
        "a++",
        "a?+",
        "a{2}+",
        "(?:fo){e<=1}",
        "a{x}",
        "(?x)a b",
        "[[:alpha:]]",
        "[abc",
        r"(?#c)x",
        "(?V1)a",
    ],
)
def test_block_search_rejects_context_patterns(pattern: str) -> None:
    assert not search_tools_module._can_search_blocks(pattern)


def _grep(
    root: Path,
    monkeypatch: MonkeyPatch,
    arguments: dict[str, object],
    *,
    blocks: bool,
    max_output_lines: int,
) -> ToolResult:
    with monkeypatch.context() as patch:
        if not blocks:
            patch.setattr(search_tools_module, "_build_block_matcher", lambda *_a, **_kw: None)
        return run_tool(
            GrepTool(_scanner_backend="python"),
            arguments,
            ToolContext(cwd=root, max_output_lines=max_output_lines),
        )


@pytest.mark.parametrize("seed", range(30))
def test_block_search_keeps_results(tmp_path: Path, monkeypatch: MonkeyPatch, seed: int) -> None:
    generator = random.Random(seed)
    monkeypatch.setattr(search_tools_module, "_PYTHON_GREP_CHUNK_BYTES", generator.randint(3, 40))
    for index in range(3):
        lines = [generator.choice(_LINES) for _ in range(generator.randint(0, 40))]
        text = "".join(line + generator.choice(_SEPARATORS) for line in lines)
        (tmp_path / f"{index}.txt").write_text(text, encoding="utf-8", newline="")

    for pattern, ignore_case, literal in _PATTERNS:
        arguments: dict[str, object] = {
            "pattern": pattern,
            "ignore_case": ignore_case,
            "literal": literal,
            "context": generator.choice((0, 0, 1, 2, 5)),
            "max_results": generator.choice((1, 3, 100)),
        }
        max_output_lines = generator.choice((4, 2_000))
        expected = _grep(
            tmp_path, monkeypatch, arguments, blocks=False, max_output_lines=max_output_lines
        )
        actual = _grep(
            tmp_path, monkeypatch, arguments, blocks=True, max_output_lines=max_output_lines
        )

        assert actual == expected, arguments


def test_possessive_match_is_kept(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    # In a block, [^x]*+ would consume the joining newline and could not give it back.
    (tmp_path / "notes.txt").write_text("foo\nx\n", encoding="utf-8")

    result = run_tool(
        GrepTool(_scanner_backend="python"), {"pattern": "^[^x]*+$"}, ToolContext(cwd=tmp_path)
    )

    assert result.text == "notes.txt:1:foo"


def test_missed_blocks_skip_line_search(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    (tmp_path / "notes.txt").write_text("one\ntwo\nthree\n" * 100, encoding="utf-8")
    searched: list[str] = []
    original = search_tools_module._build_matcher

    def counting_matcher(pattern: str, *, ignore_case: bool, literal: bool) -> object:
        matcher = original(pattern, ignore_case=ignore_case, literal=literal)

        def count(line: str) -> bool:
            searched.append(line)
            return matcher(line)

        return count

    monkeypatch.setattr(search_tools_module, "_build_matcher", counting_matcher)

    result = run_tool(
        GrepTool(_scanner_backend="python"), {"pattern": r"absent\d"}, ToolContext(cwd=tmp_path)
    )

    assert result.text == "No matches"
    assert searched == []


def test_block_timeout_falls_back_to_lines(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    block_searches = 0

    class _SlowOnBlocks:
        def search(
            self,
            text: str,
            pos: None = None,
            endpos: None = None,
            concurrent: bool | None = None,
            partial: bool = False,
            timeout: float | None = None,
        ) -> object | None:
            nonlocal block_searches
            if "\n" in text:
                block_searches += 1
                raise TimeoutError
            return object() if text.startswith("match") else None

    monkeypatch.setattr(search_tools_module, "_PYTHON_GREP_CHUNK_BYTES", 16)
    monkeypatch.setattr(
        search_tools_module.bounded_regex, "compile", lambda *_a, **_kw: _SlowOnBlocks()
    )
    (tmp_path / "notes.txt").write_text("other\nmatch 1\nother\nmatch 2\n" * 4, "utf-8")

    result = run_tool(GrepTool(), {"pattern": "match"}, ToolContext(cwd=tmp_path))

    assert result.data is not None
    assert result.data["count"] == 8
    # After one block times out, the rest of the search goes line by line.
    assert block_searches == 1
