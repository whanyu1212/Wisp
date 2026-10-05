from __future__ import annotations

import shutil
import sys
import threading
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
from wisp.tools.shell import supervisor as process_manager_module


def test_grep_regex_timeout_is_actionable(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class _TimingOutExpression:
        def search(self, *_args: object, **_kwargs: object) -> object:
            raise TimeoutError

    (tmp_path / "notes.txt").write_text("text\n", encoding="utf-8")
    monkeypatch.setattr(
        search_tools_module.bounded_regex, "compile", lambda *_a, **_kw: _TimingOutExpression()
    )

    with pytest.raises(ToolError) as caught:
        run_tool(GrepTool(), {"pattern": "(text)+"}, ToolContext(cwd=tmp_path))

    assert caught.value.failure_code == "invalid_pattern"
    assert caught.value.retryable is True
    assert caught.value.recovery_hint == "Retry with literal=true or a simpler regex pattern."


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

    result = run_tool(
        GrepTool(),
        {"pattern": "needle", "path": ".", "literal": True},
        ToolContext(cwd=tmp_path),
    )

    assert result.text == "No matches"
    assert result.data == {"count": 0, "matches": []}


def test_grep_tool_ripgrep_treats_option_like_pattern_as_literal(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    (tmp_path / "data.txt").write_text("--help\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "--help", "path": ".", "literal": True},
        context,
    )

    assert result.text == "data.txt:1:--help"
    assert "Usage:" not in result.text


def test_grep_tool_ripgrep_includes_filename_for_single_file_search(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    (tmp_path / "data.txt").write_text("match\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "match", "path": "data.txt", "literal": True},
        context,
    )

    assert result.text == "data.txt:1:match"


@pytest.mark.skip(reason="ripgrep backend removed for descriptor-safe traversal")
def test_grep_tool_ripgrep_bounds_stdout_before_buffering(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], int, int | None, int | None, int | None, int | None]] = []

    async def fake_run(
        command: list[str],
        *,
        cwd: Path,
        process_supervisor: object,
        max_stdout_lines: int,
        stdout_line_filter: object = None,
        stdout_count_filter: object = None,
        max_buffered_stdout_bytes: int | None = None,
        max_buffered_stdout_lines: int | None = None,
        max_buffered_stderr_bytes: int | None = None,
        max_buffered_stderr_lines: int | None = None,
    ) -> search_tools_module.ProcessResult:
        assert cwd == tmp_path
        assert isinstance(process_supervisor, process_manager_module.ProcessSupervisor)
        assert callable(stdout_count_filter)
        calls.append(
            (
                command,
                max_stdout_lines,
                max_buffered_stdout_bytes,
                max_buffered_stdout_lines,
                max_buffered_stderr_bytes,
                max_buffered_stderr_lines,
            )
        )
        return search_tools_module.ProcessResult(
            exit_code=-9,
            stdout="one.txt:1:e\ntwo.txt:1:e\nthree.txt:1:e\n",
            stderr="",
            stdout_truncated=True,
        )

    monkeypatch.setattr(search_tools_module.shutil, "which", lambda _name: "rg")
    monkeypatch.setattr(search_tools_module, "_run_exec_limited_stdout", fake_run)
    # This test asserts the exact rg argv for stdout bounding; opt out of the
    # protected-path default so its --glob exclusions don't clutter the assertion.
    context = ToolContext(cwd=tmp_path, protected_paths=())

    result = run_tool(GrepTool(), {"pattern": "e", "path": ".", "max_results": 2}, context)

    assert calls == [
        (
            [
                "rg",
                "--no-config",
                "--no-follow",
                "--line-number",
                "--no-heading",
                "--color=never",
                "--with-filename",
                "--field-match-separator",
                "\x1f",
                "--field-context-separator",
                "\x1e",
                "--max-columns",
                "50000",
                "--",
                "e",
                ".",
            ],
            3,
            50000,
            2000,
            50000,
            2000,
        )
    ]
    assert result.text == "one.txt:1:e\ntwo.txt:1:e\n[truncated]"
    assert result.truncated is True


def test_grep_tool_ripgrep_bounds_long_lines_before_buffering(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
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


def test_grep_tool_ripgrep_preserves_oversized_match_when_truncated(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
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


def test_grep_tool_ripgrep_counts_match_when_budget_cuts_record_prefix(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
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


def test_grep_tool_ripgrep_bounds_context_output_before_buffering(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
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


def test_grep_tool_ripgrep_does_not_count_context_text_as_match(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
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


def test_grep_tool_ripgrep_counts_matches_separately_from_context_lines(
    tmp_path: Path,
) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
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


@pytest.mark.skip(reason="ripgrep backend removed for descriptor-safe traversal")
def test_grep_tool_ripgrep_drops_context_for_omitted_merged_match(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def fake_run(
        command: list[str],
        *,
        cwd: Path,
        process_supervisor: object,
        max_stdout_lines: int,
        stdout_line_filter: object = None,
        stdout_count_filter: object = None,
        max_buffered_stdout_bytes: int | None = None,
        max_buffered_stdout_lines: int | None = None,
        max_buffered_stderr_bytes: int | None = None,
        max_buffered_stderr_lines: int | None = None,
    ) -> search_tools_module.ProcessResult:
        assert cwd == tmp_path
        assert isinstance(process_supervisor, process_manager_module.ProcessSupervisor)
        assert max_stdout_lines == 2
        assert callable(stdout_count_filter)
        assert max_buffered_stdout_bytes == 50000
        assert max_buffered_stdout_lines == 2000
        assert max_buffered_stderr_bytes == 50000
        assert max_buffered_stderr_lines == 2000
        return search_tools_module.ProcessResult(
            exit_code=-9,
            stdout=(
                "data.txt\x1f1\x1fneedle one\n"
                "data.txt\x1e2\x1ebridge\n"
                "data.txt\x1f3\x1fneedle two\n"
            ),
            stderr="",
            stdout_truncated=True,
        )

    monkeypatch.setattr(search_tools_module.shutil, "which", lambda _name: "rg")
    monkeypatch.setattr(search_tools_module, "_run_exec_limited_stdout", fake_run)
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "needle", "path": ".", "context": 1, "literal": True, "max_results": 1},
        context,
    )

    assert result.text == "data.txt:1:needle one\n[truncated]"
    assert result.data["matches"] == ["data.txt:1:needle one"]
    assert result.data["count"] == 1
    assert result.truncated is True


def test_grep_tool_ripgrep_preserves_whitespace_only_patterns(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    (tmp_path / "data.txt").write_text("a b\nab\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": " ", "path": ".", "literal": True},
        context,
    )

    assert result.text == "data.txt:1:a b"


def test_grep_tool_ripgrep_ignores_config_that_follows_symlinks(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    config = tmp_path / "ripgreprc"
    config.write_text("--follow\n", encoding="utf-8")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(config))
    context = ToolContext(cwd=workspace)

    result = run_tool(GrepTool(), {"pattern": "secret", "path": ".", "literal": True}, context)

    assert result.text == "No matches"
    assert result.data == {"count": 0, "matches": []}


def test_grep_tool_skips_symlinked_files_when_opted_out(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    context = ToolContext(cwd=workspace, allow_outside_cwd=True)

    result = run_tool(GrepTool(), {"pattern": "secret", "path": ".", "literal": True}, context)

    assert result.text == "No matches"
    assert result.data == {"count": 0, "matches": []}


def test_grep_tool_python_fallback_skips_symlinked_files_outside_cwd(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    context = ToolContext(cwd=workspace)

    result = run_tool(GrepTool(), {"pattern": "secret", "path": ".", "literal": True}, context)

    assert result.text == "No matches"
    assert result.data == {"count": 0, "matches": []}


def test_grep_tool_python_fallback_skips_symlinked_files_when_opted_out(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    context = ToolContext(cwd=workspace, allow_outside_cwd=True)

    result = run_tool(GrepTool(), {"pattern": "secret", "path": ".", "literal": True}, context)

    assert result.text == "No matches"
    assert result.data == {"count": 0, "matches": []}


def test_grep_tool_python_fallback_does_not_count_context_text_as_match(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
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


def test_grep_tool_python_fallback_counts_matches_separately_from_context_lines(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
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
    original = search_tools_module._result_from_grep_lines

    def tracking_result(lines: Sequence[str], **kwargs: object) -> ToolResult:
        collected_line_counts.append(len(lines))
        return original(lines, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(search_tools_module, "_result_from_grep_lines", tracking_result)
    context = ToolContext(cwd=tmp_path, max_output_lines=5)

    result = run_tool(
        GrepTool(),
        {"pattern": "match", "path": ".", "context": 100, "literal": True},
        context,
    )

    assert collected_line_counts == [context.max_output_lines + 1]
    assert result.truncated is True


def test_grep_tool_python_fallback_preserves_whitespace_only_patterns(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "data.txt").write_text("\tindent\nplain\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "\t", "path": ".", "literal": True},
        context,
    )

    assert result.text == "data.txt:1:\tindent"


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

    assert result.text == "first.txt:1:match\n[truncated]"
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


def test_grep_tool_python_fallback_does_not_truncate_exact_limit(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "data.txt").write_text("match\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "match", "path": ".", "literal": True, "max_results": 1},
        context,
    )

    assert result.text == "data.txt:1:match"
    assert result.truncated is False


def test_grep_tool_python_fallback_truncates_when_another_match_exists(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "data.txt").write_text("match\nmatch\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "match", "path": ".", "literal": True, "max_results": 1},
        context,
    )

    assert result.text == "data.txt:1:match\n[truncated]"
    assert result.truncated is True


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


def test_python_grep_splitlines_preserves_crlf_across_chunks(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    path = tmp_path / "data.txt"
    path.write_bytes(b"one\r\ntwo\rthree")
    monkeypatch.setattr(search_tools_module, "_PYTHON_GREP_CHUNK_BYTES", 4)

    assert list(search_tools_module._iter_utf8_splitlines(path)) == ["one", "two", "three"]


def test_python_grep_splitlines_scans_long_lines_once_per_chunk(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    path = tmp_path / "data.txt"
    path.write_text("x" * 100, encoding="utf-8")
    monkeypatch.setattr(search_tools_module, "_PYTHON_GREP_CHUNK_BYTES", 8)
    scanned_chars = 0
    original = search_tools_module._yield_splitline_chunk

    def tracking_chunk(
        text: str,
        line_parts: list[str],
        *,
        max_line_chars: int,
        final: bool = False,
    ) -> object:
        nonlocal scanned_chars
        scanned_chars += len(text)
        return original(text, line_parts, max_line_chars=max_line_chars, final=final)

    monkeypatch.setattr(search_tools_module, "_yield_splitline_chunk", tracking_chunk)

    assert list(search_tools_module._iter_utf8_splitlines(path)) == ["x" * 100]
    assert scanned_chars == 100


def test_python_grep_splitlines_bounds_a_line_ending_in_the_current_chunk(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data.txt"
    path.write_text("x" * 11 + "\n", encoding="utf-8")

    with pytest.raises(ToolError, match="line longer than 10 characters"):
        list(search_tools_module._iter_utf8_splitlines(path, max_line_chars=10))
