from __future__ import annotations

import errno
import os
import sys
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from tests.tools.support import run_tool
from wisp.tools.builtin import GrepTool
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.search import tools as search_tools_module

_LIMIT_HINT = (
    "[truncated] Stopped at max_results=1. Narrow the search, or raise max_results to see more."
)


def test_grep_regex_timeout_is_actionable(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    timeouts: list[float] = []
    holds_gil: list[bool] = []

    class _TimingOutExpression:
        def search(
            self,
            _line: str,
            pos: None = None,
            endpos: None = None,
            concurrent: bool | None = None,
            partial: bool = False,
            timeout: float | None = None,
        ) -> object:
            timeouts.append(timeout)
            holds_gil.append(concurrent is False)
            raise TimeoutError

    (tmp_path / "notes.txt").write_text("text\n", encoding="utf-8")
    monkeypatch.setattr(
        search_tools_module.bounded_regex, "compile", lambda *_a, **_kw: _TimingOutExpression()
    )
    monkeypatch.setattr(search_tools_module, "_build_block_matcher", lambda *_a, **_kw: None)

    with pytest.raises(ToolError) as caught:
        run_tool(GrepTool(), {"pattern": "(text)+"}, ToolContext(cwd=tmp_path))

    assert caught.value.failure_code == "invalid_pattern"
    assert caught.value.retryable is True
    assert caught.value.recovery_hint == "Retry with literal=true or a simpler regex pattern."
    assert timeouts == [
        search_tools_module._REGEX_TIMEOUT_SECONDS,
        search_tools_module._REGEX_RETRY_BUDGET_SECONDS,
    ]
    # Only the short attempt may hold the GIL; the long retry must let other threads run.
    assert holds_gil == [True, False]


def test_grep_times_out_real_backtracking(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    # Uses the real regex module, so the positional search arguments must reach its timeout.
    monkeypatch.setattr(search_tools_module, "_REGEX_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(search_tools_module, "_REGEX_RETRY_BUDGET_SECONDS", 0.05)
    # Unbounded, this search takes about a second; bounded, it stops within 60 ms.
    (tmp_path / "notes.txt").write_text("a" * 30 + "\n", encoding="utf-8")

    with pytest.raises(ToolError, match="regex evaluation time limit"):
        run_tool(GrepTool(), {"pattern": "(a|aa)+b"}, ToolContext(cwd=tmp_path))


def test_grep_retries_lines_that_time_out_while_starved(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    # Simulates a CPU-starved thread: every first attempt misses the wall-clock limit, but
    # the line itself is cheap, so the longer retry succeeds and spends almost no budget.
    timeouts: list[float] = []

    class _StarvedExpression:
        def search(
            self,
            line: str,
            pos: None = None,
            endpos: None = None,
            concurrent: bool | None = None,
            partial: bool = False,
            timeout: float | None = None,
        ) -> object | None:
            timeouts.append(timeout)
            if timeout == search_tools_module._REGEX_TIMEOUT_SECONDS:
                raise TimeoutError
            return object() if line.startswith("match") else None

    lines = [f"match {index}" if index % 2 == 0 else f"other {index}" for index in range(50)]
    (tmp_path / "notes.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        search_tools_module.bounded_regex, "compile", lambda *_a, **_kw: _StarvedExpression()
    )
    monkeypatch.setattr(search_tools_module, "_build_block_matcher", lambda *_a, **_kw: None)

    result = run_tool(GrepTool(), {"pattern": "^match"}, ToolContext(cwd=tmp_path))

    assert result.data is not None
    assert result.data["count"] == 25
    assert len(timeouts) == 100
    retry_timeouts = timeouts[1::2]
    assert retry_timeouts[0] == search_tools_module._REGEX_RETRY_BUDGET_SECONDS
    assert all(
        search_tools_module._REGEX_TIMEOUT_SECONDS < retry_timeout
        for retry_timeout in retry_timeouts
    )


def test_grep_fails_without_retry_once_the_retry_budget_is_spent(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    # A pattern that is slow on every line but never exceeds the retry window must not
    # turn one search into an unbounded scan.
    timeouts: list[float] = []

    class _SlowExpression:
        def search(
            self,
            _line: str,
            pos: None = None,
            endpos: None = None,
            concurrent: bool | None = None,
            partial: bool = False,
            timeout: float | None = None,
        ) -> object | None:
            timeouts.append(timeout)
            if timeout == search_tools_module._REGEX_TIMEOUT_SECONDS:
                raise TimeoutError
            time.sleep(0.03)
            return None

    (tmp_path / "notes.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    monkeypatch.setattr(search_tools_module, "_REGEX_RETRY_BUDGET_SECONDS", 0.02)
    monkeypatch.setattr(
        search_tools_module.bounded_regex, "compile", lambda *_a, **_kw: _SlowExpression()
    )
    monkeypatch.setattr(search_tools_module, "_build_block_matcher", lambda *_a, **_kw: None)

    with pytest.raises(ToolError, match="regex evaluation time limit") as caught:
        run_tool(GrepTool(), {"pattern": "slow"}, ToolContext(cwd=tmp_path))

    assert caught.value.failure_code == "invalid_pattern"
    assert timeouts == [
        search_tools_module._REGEX_TIMEOUT_SECONDS,
        0.02,
        search_tools_module._REGEX_TIMEOUT_SECONDS,
    ]


def test_native_grep_loader_only_falls_back_for_absent_extension(
    monkeypatch: MonkeyPatch,
) -> None:
    def missing_extension(_name: str) -> object:
        raise ModuleNotFoundError("No module named 'wisp._native'", name="wisp._native")

    monkeypatch.setattr(search_tools_module, "import_module", missing_extension)
    assert search_tools_module._load_native_grep() is None

    def broken_dependency(_name: str) -> object:
        raise ModuleNotFoundError("No module named 'broken'", name="broken")

    monkeypatch.setattr(search_tools_module, "import_module", broken_dependency)
    with pytest.raises(ModuleNotFoundError, match="broken"):
        search_tools_module._load_native_grep()


def test_native_grep_selection_preserves_python_only_matching_modes(
    monkeypatch: MonkeyPatch,
) -> None:
    sentinel = object()
    monkeypatch.setattr(search_tools_module, "_NATIVE_GREP", sentinel)

    assert (
        search_tools_module._select_native_grep("auto", literal=True, ignore_case=False) is sentinel
    )
    assert (
        search_tools_module._select_native_grep("python", literal=True, ignore_case=False) is None
    )
    assert (
        search_tools_module._select_native_grep("native", literal=False, ignore_case=False) is None
    )
    assert search_tools_module._select_native_grep("native", literal=True, ignore_case=True) is None


def test_native_grep_explicit_backend_requires_installed_extension(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(search_tools_module, "_NATIVE_GREP", None)

    with pytest.raises(RuntimeError, match="native grep backend is not installed"):
        search_tools_module._select_native_grep("native", literal=True, ignore_case=False)


def test_native_grep_rejects_inputs_that_pyO3_cannot_represent() -> None:
    assert search_tools_module._native_arguments_supported("needle", 0, sys.maxsize)
    assert not search_tools_module._native_arguments_supported("\udcff", 1)
    assert not search_tools_module._native_arguments_supported("needle", -1)
    assert not search_tools_module._native_arguments_supported("needle", sys.maxsize + 1)


@pytest.mark.parametrize(
    "native_error", [OSError("/proc unavailable"), RuntimeError("unsupported")]
)
def test_native_grep_falls_back_when_descriptor_scanning_is_unavailable(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    native_error: Exception,
) -> None:
    class Cancellation:
        def cancel(self) -> None:
            pass

    class UnavailableNative:
        def __init__(self) -> None:
            self.calls = 0

        def GrepCancellation(self) -> Cancellation:
            return Cancellation()

        def scan_literal_fd(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            raise native_error

    native = UnavailableNative()
    monkeypatch.setattr(search_tools_module, "_NATIVE_GREP", native)
    (tmp_path / "data.txt").write_text("before\nneedle\nafter\n", encoding="utf-8")

    result = run_tool(
        GrepTool(_scanner_backend="native"),
        {"pattern": "needle", "literal": True},
        ToolContext(cwd=tmp_path),
    )

    assert native.calls == 1
    assert result.text == "data.txt:2:needle"


def test_grep_tool_python_fallback_supports_literal_ignore_case_and_glob(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "alpha.txt").write_text("Needle here\n", encoding="utf-8")
    (tmp_path / "beta.md").write_text("needle elsewhere\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {
            "pattern": "needle",
            "path": ".",
            "glob": "*.txt",
            "literal": True,
            "ignore_case": True,
        },
        context,
    )

    assert result.data["count"] == 1
    assert result.text == "alpha.txt:1:Needle here"


def test_grep_tool_prepares_glob_once_and_reuses_display_path(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    path = tmp_path / "alpha.txt"
    path.write_text("before\nneedle\nafter\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)
    expanded: list[str] = []
    displayed: list[Path] = []
    original_expand = search_tools_module._expand_brace_alternatives
    original_display = search_tools_module._display_walked_path

    def tracking_expand(pattern: str) -> tuple[str, ...]:
        expanded.append(pattern)
        return original_expand(pattern)

    def tracking_display(candidate: Path, resolved_cwd: Path) -> str:
        displayed.append(candidate)
        return original_display(candidate, resolved_cwd)

    monkeypatch.setattr(search_tools_module, "_expand_brace_alternatives", tracking_expand)
    monkeypatch.setattr(search_tools_module, "_display_walked_path", tracking_display)

    result = run_tool(
        GrepTool(),
        {
            "pattern": "needle",
            "path": ".",
            "glob": "*.{txt,md}",
            "literal": True,
            "context": 1,
        },
        context,
    )

    assert result.text == "alpha.txt-1-before\nalpha.txt:2:needle\nalpha.txt-3-after"
    assert result.data["matches"] == [
        "alpha.txt-1-before",
        "alpha.txt:2:needle",
        "alpha.txt-3-after",
    ]
    assert expanded == ["*.{txt,md}"]
    assert displayed == [path]


def test_grep_tool_no_match_without_glob_skips_display_path_work(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    (tmp_path / "alpha.txt").write_text("haystack\n", encoding="utf-8")

    def unexpected_display(_candidate: Path, _resolved_cwd: Path) -> str:
        raise AssertionError("a no-match grep should not compute a display path")

    monkeypatch.setattr(search_tools_module, "_display_walked_path", unexpected_display)

    # The native scanner formats records itself, so it takes the display path before
    # scanning; only the Python scanner can defer it until a line matches.
    result = run_tool(
        GrepTool(_scanner_backend="python"),
        {"pattern": "needle", "path": ".", "literal": True},
        ToolContext(cwd=tmp_path),
    )

    assert result.text == "No matches"
    assert result.data == {"count": 0, "matches": []}


def test_grep_treats_option_like_pattern_as_literal(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("--help\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "--help", "path": ".", "literal": True},
        context,
    )

    assert result.text == "data.txt:1:--help"
    assert "Usage:" not in result.text


def test_grep_includes_filename_for_single_file_search(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("match\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "match", "path": "data.txt", "literal": True},
        context,
    )

    assert result.text == "data.txt:1:match"


def test_grep_bounds_long_lines_before_buffering(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("needle" + ("x" * 10_000) + "\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_output_bytes=80)

    result = run_tool(
        GrepTool(),
        {"pattern": "needle", "path": ".", "literal": True},
        context,
    )

    assert result.text.startswith("data.txt:1:needle")
    assert result.truncated is True
    assert len(result.text.encode("utf-8")) <= context.max_output_bytes


def test_grep_preserves_oversized_match_when_truncated(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("needle" + ("x" * 10_000) + "\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_output_bytes=35)

    result = run_tool(
        GrepTool(),
        {"pattern": "needle", "path": ".", "literal": True},
        context,
    )

    assert result.text != "No matches"
    assert result.text.startswith("data.txt:1:")
    assert result.text.endswith("[truncated]")
    assert result.data["count"] == 1
    assert result.truncated is True
    assert len(result.text.encode("utf-8")) <= context.max_output_bytes


def test_grep_counts_match_when_budget_cuts_record_prefix(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("needle" + ("x" * 10_000) + "\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_output_bytes=5)

    result = run_tool(
        GrepTool(),
        {"pattern": "needle", "path": ".", "literal": True},
        context,
    )

    assert result.text != "No matches"
    assert result.data["count"] == 1
    assert result.truncated is True
    assert len(result.text.encode("utf-8")) <= context.max_output_bytes


def test_grep_bounds_context_output_before_buffering(tmp_path: Path) -> None:
    lines = [f"before {index}" for index in range(50)]
    lines.append("needle")
    lines.extend(f"after {index}" for index in range(50))
    (tmp_path / "data.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_output_lines=10)

    result = run_tool(
        GrepTool(),
        {"pattern": "needle", "path": ".", "context": 1000, "literal": True},
        context,
    )

    assert len(result.text.splitlines()) <= context.max_output_lines
    assert "data.txt:51:needle" in result.text
    assert "data.txt-1-before 0" not in result.text
    assert result.text.endswith("[truncated]")
    assert result.truncated is True


def test_grep_does_not_count_context_text_as_match(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("context :123: text\nneedle\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "needle", "path": ".", "context": 1, "literal": True, "max_results": 1},
        context,
    )

    assert "data.txt-1-context :123: text" in result.text
    assert "data.txt:2:needle" in result.text
    assert result.data["count"] == 1


def test_grep_counts_matches_separately_from_context_lines(
    tmp_path: Path,
) -> None:
    (tmp_path / "data.txt").write_text("before\nmatch\nafter\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "match", "path": ".", "context": 1, "literal": True, "max_results": 1},
        context,
    )

    assert "data.txt-1-before" in result.text
    assert "data.txt:2:match" in result.text
    assert "data.txt-3-after" in result.text


@pytest.mark.parametrize(
    ("content", "pattern", "expected"),
    [
        pytest.param("a b\nab\n", " ", "data.txt:1:a b", id="space"),
        pytest.param("\tindent\nplain\n", "\t", "data.txt:1:\tindent", id="tab"),
    ],
)
def test_grep_preserves_whitespace_only_patterns(
    tmp_path: Path, content: str, pattern: str, expected: str
) -> None:
    (tmp_path / "data.txt").write_text(content, encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": pattern, "path": ".", "literal": True},
        context,
    )

    assert result.text == expected


@pytest.mark.parametrize("allow_outside_cwd", [False, True], ids=["confined", "opted-out"])
def test_grep_skips_symlinked_files(tmp_path: Path, allow_outside_cwd: bool) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    context = ToolContext(cwd=workspace, allow_outside_cwd=allow_outside_cwd)

    result = run_tool(GrepTool(), {"pattern": "secret", "path": ".", "literal": True}, context)

    assert result.text == "No matches"
    assert result.data == {"count": 0, "matches": []}


def test_grep_tool_python_fallback_counts_match_when_path_contains_separator(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    package_dir = tmp_path / "pkg-1-test"
    package_dir.mkdir()
    (package_dir / "a.txt").write_text("needle\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "needle", "path": ".", "literal": True},
        context,
    )

    assert result.text == "pkg-1-test/a.txt:1:needle"
    assert result.data["count"] == 1


def test_grep_tool_python_fallback_merges_overlapping_context_groups(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "data.txt").write_text(
        "before\nmatch one\nmatch two\nafter\n",
        encoding="utf-8",
    )

    result = run_tool(
        GrepTool(),
        {"pattern": "match", "path": ".", "context": 1, "literal": True},
        ToolContext(cwd=tmp_path),
    )

    assert result.text == (
        "data.txt-1-before\ndata.txt:2:match one\ndata.txt:3:match two\ndata.txt-4-after"
    )
    assert result.data["count"] == 2


def test_grep_tool_python_fallback_bounds_context_collection(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "data.txt").write_text("match\n" * 100, encoding="utf-8")
    collected_line_counts: list[int] = []
    original = search_tools_module._grep_result

    def tracking_result(records: Sequence[str], **kwargs: object) -> ToolResult:
        collected_line_counts.append(len(records))
        return original(records, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(search_tools_module, "_grep_result", tracking_result)
    context = ToolContext(cwd=tmp_path, max_output_lines=5)

    result = run_tool(
        GrepTool(),
        {"pattern": "match", "path": ".", "context": 100, "literal": True},
        context,
    )

    assert collected_line_counts == [context.max_output_lines + 1]
    assert result.truncated is True


def test_grep_tool_reports_actionable_invalid_pattern(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)

    with pytest.raises(ToolError, match="Invalid grep pattern") as raised:
        run_tool(GrepTool(), {"pattern": "unclosed("}, context)

    assert raised.value.failure_code == "invalid_pattern"
    assert raised.value.retryable is True
    assert raised.value.recovery_hint == "Retry with literal=true when searching for exact text."


def test_grep_tool_rejects_empty_pattern(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)

    with pytest.raises(ToolError, match="pattern must not be empty"):
        run_tool(GrepTool(), {"pattern": ""}, context)


def test_grep_tool_python_fallback_stops_after_extra_match(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    first = tmp_path / "first.txt"
    first.write_text("match\nmatch\n", encoding="utf-8")
    visited_later = False

    def files(
        _path: Path,
        _context: ToolContext,
        **_kwargs: object,
    ) -> Iterable[search_tools_module._WalkedFile]:
        nonlocal visited_later
        yield search_tools_module._WalkedFile(first)
        visited_later = True
        yield search_tools_module._WalkedFile(tmp_path / "later.txt")

    monkeypatch.setattr(search_tools_module, "_iter_walked_files", files)
    result = run_tool(
        GrepTool(),
        {"pattern": "match", "literal": True, "max_results": 1},
        ToolContext(cwd=tmp_path),
    )

    assert result.text == "first.txt:1:match\n" + _LIMIT_HINT
    assert result.truncated is True
    assert visited_later is False


def test_grep_tool_python_fallback_does_not_materialize_file(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "data.txt").write_text("needle\n", encoding="utf-8")

    def fail_read_text(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("Python grep must stream files")

    monkeypatch.setattr(Path, "read_text", fail_read_text)
    result = run_tool(
        GrepTool(),
        {"pattern": "needle", "literal": True},
        ToolContext(cwd=tmp_path),
    )

    assert result.text == "data.txt:1:needle"


def test_grep_tool_python_fallback_runs_off_event_loop(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "data.txt").write_text("needle\n", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()
    original = search_tools_module._python_grep

    def blocking_grep(**kwargs: object) -> ToolResult:
        started.set()
        release.wait(timeout=2)
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(search_tools_module, "_python_grep", blocking_grep)

    async def scenario() -> bool:
        completed = anyio.Event()

        async def grep_files() -> None:
            await GrepTool().run({"pattern": "needle", "literal": True}, ToolContext(cwd=tmp_path))
            completed.set()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(grep_files)
            assert await anyio.to_thread.run_sync(started.wait, 1)
            await anyio.sleep(0)
            responsive = not completed.is_set()
            release.set()
        return responsive

    assert anyio.run(scenario) is True


def test_grep_tool_python_fallback_abandons_worker_wait_on_cancel(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "data.txt").write_text("needle\n", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()

    def blocking_grep(**_kwargs: object) -> ToolResult:
        started.set()
        release.wait(timeout=2)
        return ToolResult(text="No matches")

    monkeypatch.setattr(search_tools_module, "_python_grep", blocking_grep)

    async def scenario() -> None:
        async def grep_files() -> None:
            await GrepTool().run({"pattern": "needle", "literal": True}, ToolContext(cwd=tmp_path))

        with anyio.fail_after(0.5):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(grep_files)
                assert await anyio.to_thread.run_sync(started.wait, 1)
                task_group.cancel_scope.cancel()
        release.set()

    anyio.run(scenario)


@pytest.mark.parametrize(
    ("content", "expected_text", "truncated"),
    [
        pytest.param("match\n", "data.txt:1:match", False, id="exact-limit"),
        pytest.param(
            "match\nmatch\n", "data.txt:1:match\n" + _LIMIT_HINT, True, id="another-match"
        ),
    ],
)
def test_grep_truncates_only_past_max_results(
    tmp_path: Path, content: str, expected_text: str, truncated: bool
) -> None:
    (tmp_path / "data.txt").write_text(content, encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "match", "path": ".", "literal": True, "max_results": 1},
        context,
    )

    assert result.text == expected_text
    assert result.truncated is truncated


def test_grep_fills_output_budget_exactly(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")
    record = "a.txt:1:needle"

    result = run_tool(
        GrepTool(),
        {"pattern": "needle", "path": ".", "literal": True},
        ToolContext(cwd=tmp_path, max_output_bytes=len(record)),
    )

    assert result.text == record
    assert not result.truncated


@pytest.mark.parametrize(
    ("context_lines", "expected_text"),
    [(0, "a.txt:1:needle\nb.txt:1:needle"), (1, "a.txt:1:needle\n--\nb.txt:1:needle")],
    ids=["no-context", "context"],
)
def test_grep_separates_files_only_with_context(
    tmp_path: Path, context_lines: int, expected_text: str
) -> None:
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("needle\n", encoding="utf-8")

    result = run_tool(
        GrepTool(_scanner_backend="python"),
        {"pattern": "needle", "path": ".", "literal": True, "context": context_lines},
        ToolContext(cwd=tmp_path, max_output_bytes=len(expected_text)),
    )

    assert result.text == expected_text
    assert not result.truncated


@pytest.mark.parametrize(
    ("glob", "found"),
    [("*.txt", {"ignored.txt", "kept.txt"}), ("!*.md", {"kept.txt"})],
    ids=["inclusion-glob", "exclusion-glob"],
)
def test_only_inclusion_glob_reincludes_ignored_files(
    tmp_path: Path, glob: str, found: set[str]
) -> None:
    (tmp_path / ".ignore").write_text("ignored.txt\n", encoding="utf-8")
    for name in ("ignored.txt", "kept.txt", "notes.md"):
        (tmp_path / name).write_text("needle\n", encoding="utf-8")

    result = run_tool(
        GrepTool(),
        {"pattern": "needle", "path": ".", "glob": glob, "literal": True},
        ToolContext(cwd=tmp_path),
    )

    assert {record.split(":")[0] for record in result.data["matches"]} == found


def test_grep_tool_python_fallback_skips_hidden_entries(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / ".env").write_text("secret\n", encoding="utf-8")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "secret.txt").write_text("secret\n", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("secret\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(GrepTool(), {"pattern": "secret", "path": ".", "literal": True}, context)

    assert result.text == "visible.txt:1:secret"
    assert result.data["matches"] == ["visible.txt:1:secret"]


def test_grep_tool_python_fallback_bounds_eof_context_to_requested_radius(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "data.txt").write_text("one\ntwo\nthree\nfour\nmatch\n", encoding="utf-8")

    result = run_tool(
        GrepTool(),
        {"pattern": "match", "literal": True, "context": 2},
        ToolContext(cwd=tmp_path),
    )

    assert result.text == "data.txt-3-three\ndata.txt-4-four\ndata.txt:5:match"


def test_grep_tool_python_fallback_discards_matches_from_invalid_utf8_file(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "data.txt").write_bytes(b"match\nmatch\n\xff")

    result = run_tool(
        GrepTool(),
        {"pattern": "match", "literal": True, "max_results": 1},
        ToolContext(cwd=tmp_path),
    )

    assert result.text == "No matches"
    assert result.data == {"count": 0, "matches": []}


def test_grep_tool_python_fallback_preserves_splitlines_boundaries(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "data.txt").write_text(
        "first\vvertical\fform\x1cfile\x1dgroup\x1erecord\x85next\u2028line\u2029paragraph",
        encoding="utf-8",
    )

    result = run_tool(
        GrepTool(),
        {"pattern": "^(vertical|form|file|group|record|next|line|paragraph)$"},
        ToolContext(cwd=tmp_path),
    )

    assert result.text.splitlines() == [
        "data.txt:2:vertical",
        "data.txt:3:form",
        "data.txt:4:file",
        "data.txt:5:group",
        "data.txt:6:record",
        "data.txt:7:next",
        "data.txt:8:line",
        "data.txt:9:paragraph",
    ]


def _lines(path: Path) -> list[str]:
    return [line for block in search_tools_module._iter_utf8_line_blocks(path) for line in block]


@pytest.mark.parametrize("chunk_bytes", range(1, 12))
@pytest.mark.parametrize(
    "text",
    [
        pytest.param("a\r\nb\r\r\nc\n\rd\x85é \r\n\nf\v\f\x1c\x1d\x1eg\r", id="separators"),
        pytest.param("one\r\ntwo\rthree", id="unterminated-last-line"),
    ],
)
def test_splitlines_matches_any_chunking(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    chunk_bytes: int,
    text: str,
) -> None:
    path = tmp_path / "data.txt"
    path.write_text(text, encoding="utf-8", newline="")
    monkeypatch.setattr(search_tools_module, "_PYTHON_GREP_CHUNK_BYTES", chunk_bytes)

    assert _lines(path) == text.splitlines()


def test_python_grep_splitlines_scans_long_lines_once_per_chunk(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    path = tmp_path / "data.txt"
    path.write_text("x" * 100, encoding="utf-8")
    monkeypatch.setattr(search_tools_module, "_PYTHON_GREP_CHUNK_BYTES", 8)
    scanned_chars = 0
    original = search_tools_module._split_chunk_lines

    def tracking_chunk(
        text: str,
        line_parts: list[str],
        lines: list[str],
        *,
        max_line_chars: int,
        final: bool = False,
    ) -> bool:
        nonlocal scanned_chars
        scanned_chars += len(text)
        return original(text, line_parts, lines, max_line_chars=max_line_chars, final=final)

    monkeypatch.setattr(search_tools_module, "_split_chunk_lines", tracking_chunk)

    assert _lines(path) == ["x" * 100]
    assert scanned_chars == 100


def test_python_grep_splitlines_bounds_a_line_ending_in_the_current_chunk(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data.txt"
    path.write_text("x" * 11 + "\n", encoding="utf-8")

    with pytest.raises(search_tools_module._OversizedLineDetected):
        list(search_tools_module._iter_utf8_line_blocks(path, max_line_chars=10))


def test_grep_skips_files_with_oversized_lines(tmp_path: Path) -> None:
    (tmp_path / "app.min.js").write_text("x" * 1_000_001 + "needle\n", encoding="utf-8")
    (tmp_path / "src.js").write_text("needle\n", encoding="utf-8")

    result = run_tool(
        GrepTool(_scanner_backend="python"), {"pattern": "needle"}, ToolContext(cwd=tmp_path)
    )

    assert result.data["matches"] == ["src.js:1:needle"]
    assert result.text == (
        "src.js:1:needle\n[skipped 1 file with a line longer than 1000000 characters: app.min.js]"
    )


def test_grep_names_oversized_target_file(tmp_path: Path) -> None:
    (tmp_path / "app.min.js").write_text("x" * 1_000_001, encoding="utf-8")

    with pytest.raises(ToolError, match="grep cannot search app.min.js: it has a line longer"):
        run_tool(
            GrepTool(_scanner_backend="python"),
            {"pattern": "needle", "path": "app.min.js"},
            ToolContext(cwd=tmp_path),
        )


def test_grep_keeps_skip_notice_within_budget(tmp_path: Path) -> None:
    (tmp_path / "a.min.js").write_text("x" * 1_000_001, encoding="utf-8")
    for name in ("b.js", "c.js", "d.js"):
        (tmp_path / name).write_text("needle\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_output_lines=2)

    result = run_tool(GrepTool(_scanner_backend="python"), {"pattern": "needle"}, context)

    assert result.truncated is True
    assert result.text.splitlines()[-1].startswith("[skipped 1 file")


def _make_unreadable(path: Path, failure: str, monkeypatch: MonkeyPatch) -> str:
    """Make ``path`` fail to open or to read, returning the reason grep reports."""

    if failure == "open":
        if os.name == "nt" or os.geteuid() == 0:
            pytest.skip("needs POSIX permissions that apply to the current user")
        path.chmod(0o000)
        return os.strerror(errno.EACCES)
    unreadable = path.stat().st_ino
    original = search_tools_module._iter_utf8_line_blocks

    def fail_read(descriptor: int, **kwargs: int) -> Iterable[list[str]]:
        if os.fstat(descriptor).st_ino == unreadable:
            # What reading an iCloud placeholder file raises on macOS.
            raise OSError(errno.EDEADLK, os.strerror(errno.EDEADLK))
        return original(descriptor, **kwargs)

    monkeypatch.setattr(search_tools_module, "_iter_utf8_line_blocks", fail_read)
    return os.strerror(errno.EDEADLK)


@pytest.mark.parametrize("failure", ["open", "read"])
def test_grep_skips_unreadable_files(
    tmp_path: Path, monkeypatch: MonkeyPatch, failure: str
) -> None:
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "c.min.js").write_text("x" * 1_000_001, encoding="utf-8")
    reason = _make_unreadable(tmp_path / "b.txt", failure, monkeypatch)

    result = run_tool(
        GrepTool(_scanner_backend="python"), {"pattern": "needle"}, ToolContext(cwd=tmp_path)
    )

    assert result.data["matches"] == ["a.txt:1:needle"]
    assert result.text == (
        "a.txt:1:needle\n"
        "[skipped 1 file with a line longer than 1000000 characters: c.min.js]\n"
        f"[skipped 1 file that could not be read: b.txt ({reason})]"
    )


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("open", "Could not open file b.txt: .*{reason}"),
        ("read", "grep cannot read b.txt: {reason}"),
    ],
)
def test_grep_names_unreadable_target_file(
    tmp_path: Path, monkeypatch: MonkeyPatch, failure: str, message: str
) -> None:
    (tmp_path / "b.txt").write_text("needle\n", encoding="utf-8")
    reason = _make_unreadable(tmp_path / "b.txt", failure, monkeypatch)

    with pytest.raises(ToolError, match=message.format(reason=reason)):
        run_tool(
            GrepTool(_scanner_backend="python"),
            {"pattern": "needle", "path": "b.txt"},
            ToolContext(cwd=tmp_path),
        )


def test_grep_clips_long_lines(tmp_path: Path) -> None:
    (tmp_path / "min.js").write_text("x" * 600 + "needle\n", encoding="utf-8")

    result = run_tool(
        GrepTool(_scanner_backend="python"), {"pattern": "needle"}, ToolContext(cwd=tmp_path)
    )

    assert result.text == "min.js:1:" + "x" * 500 + "... [106 more characters]"


def test_grep_counts_colon_file_names(tmp_path: Path) -> None:
    (tmp_path / "a:1:b.txt").write_text("needle\nother\nneedle\n", encoding="utf-8")

    result = run_tool(
        GrepTool(_scanner_backend="python"),
        {"pattern": "needle", "context": 1},
        ToolContext(cwd=tmp_path),
    )

    assert result.data["count"] == 2
    assert result.text == "a:1:b.txt:1:needle\na:1:b.txt-2-other\na:1:b.txt:3:needle"
