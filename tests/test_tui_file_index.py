"""Tests for the ``@``-picker's path collection and fuzzy ranking."""

from __future__ import annotations

from pathlib import Path

import pytest

from wisp.tools.context import ToolContext
from wisp.tui.file_index import FileIndexConfig, collect_paths, filter_paths, score_path
from wisp.tui.textual_app import _file_index_context

pytestmark = pytest.mark.tui


def _config(root: Path, **overrides: object) -> FileIndexConfig:
    return FileIndexConfig(root=root, context=ToolContext(cwd=root), **overrides)  # type: ignore[arg-type]


def _write(root: Path, relative: str, content: str = "x") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# --- collection ------------------------------------------------------------


def test_collects_nested_files_and_marks_directories(tmp_path: Path) -> None:
    _write(tmp_path, "src/wisp/app.py")
    _write(tmp_path, "README.md")

    paths = collect_paths(_config(tmp_path))

    assert "README.md" in paths
    assert "src/wisp/app.py" in paths
    # Directories are suffixed so the picker needn't re-stat to tell them apart.
    assert "src/" in paths
    assert "src/wisp/" in paths


def test_prunes_ignored_directories(tmp_path: Path) -> None:
    _write(tmp_path, "node_modules/left-pad/index.js")
    _write(tmp_path, "__pycache__/app.cpython-312.pyc")
    _write(tmp_path, ".git/config")
    _write(tmp_path, "src/app.py")

    paths = collect_paths(_config(tmp_path))

    assert paths == ("src/", "src/app.py")


def test_excludes_protected_paths(tmp_path: Path) -> None:
    """A secret's *filename* is a disclosure; it must never be @-mentionable."""

    _write(tmp_path, ".env", "SECRET=1")
    _write(tmp_path, ".env.production", "SECRET=1")
    _write(tmp_path, "config/.env.local", "SECRET=1")
    _write(tmp_path, ".env.example", "PLACEHOLDER=1")
    _write(tmp_path, "app.py")

    paths = collect_paths(_config(tmp_path))

    assert ".env" not in paths
    assert ".env.production" not in paths
    assert "config/.env.local" not in paths
    assert "app.py" in paths
    # `.env.example` holds placeholders, not secrets, and is deliberately not in
    # DEFAULT_PROTECTED_PATHS — it should stay mentionable.
    assert ".env.example" in paths


def test_respects_entry_cap(tmp_path: Path) -> None:
    for index in range(50):
        _write(tmp_path, f"file_{index:03d}.txt")

    paths = collect_paths(_config(tmp_path, max_entries=10))

    assert len(paths) == 10


def test_respects_depth_cap(tmp_path: Path) -> None:
    _write(tmp_path, "a/b/c/d/deep.txt")

    paths = collect_paths(_config(tmp_path, max_depth=2))

    assert not any(path.startswith("a/b/c") for path in paths)
    assert "a/" in paths


def test_symlinked_directory_is_listed_but_not_followed(tmp_path: Path) -> None:
    """A symlink to an ancestor would loop forever if descended into."""

    _write(tmp_path, "real/file.txt")
    (tmp_path / "loop").symlink_to(tmp_path, target_is_directory=True)

    paths = collect_paths(_config(tmp_path))

    assert "real/file.txt" in paths
    assert not any(path.startswith("loop/") for path in paths)


def test_missing_root_returns_empty(tmp_path: Path) -> None:
    assert collect_paths(_config(tmp_path / "nope")) == ()


def test_unreadable_directory_is_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "readable/file.txt")
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "hidden.txt").write_text("x", encoding="utf-8")
    blocked.chmod(0o000)
    try:
        paths = collect_paths(_config(tmp_path))
        assert "readable/file.txt" in paths
    finally:
        blocked.chmod(0o755)


# --- policy resolution -----------------------------------------------------


def test_context_honors_user_configured_protected_paths(tmp_path: Path) -> None:
    """A bare ToolContext would hardcode the defaults and leak a configured secret."""

    settings = Path.home() / ".wisp"
    settings.mkdir(parents=True, exist_ok=True)
    (settings / "settings.json").write_text(
        '{"protected_paths": [".env", "secrets.yaml"]}', encoding="utf-8"
    )
    _write(tmp_path, "secrets.yaml", "TOKEN=1")
    _write(tmp_path, "app.py")

    context = _file_index_context(tmp_path)
    paths = collect_paths(FileIndexConfig(root=tmp_path, context=context))

    assert "secrets.yaml" in context.protected_paths
    assert "secrets.yaml" not in paths
    assert "app.py" in paths


def test_context_protects_the_credential_file(tmp_path: Path) -> None:
    """ToolContext.from_config appends auth_path; a bare context would not."""

    context = _file_index_context(tmp_path)

    assert any(pattern.endswith("auth.json") for pattern in context.protected_paths)


def test_context_falls_back_to_secure_defaults(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Config resolution failing must not crash the TUI, and must not open the gate."""

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr("wisp.tui.textual_app.WispConfig.from_env", _boom)

    context = _file_index_context(tmp_path)

    assert ".env" in context.protected_paths


# --- matching --------------------------------------------------------------


def test_matches_gapped_subsequence() -> None:
    """The headline case substring matching cannot handle."""

    assert score_path("src/wisp/tui/textual_app.py", "tuiapp") is not None


def test_rejects_out_of_order_characters() -> None:
    assert score_path("src/app.py", "ppa") is None


def test_contiguous_match_outranks_scattered() -> None:
    contiguous = score_path("src/app.py", "app")
    scattered = score_path("a/p/p/zzz.py", "app")

    assert contiguous is not None and scattered is not None
    assert contiguous.score > scattered.score


def test_basename_match_outranks_directory_match() -> None:
    basename = score_path("src/app.py", "app")
    directory = score_path("app/utils/misc.py", "app")

    assert basename is not None and directory is not None
    assert basename.score > directory.score


def test_boundary_start_outranks_midword() -> None:
    boundary = score_path("src/textual_app.py", "app")
    midword = score_path("src/happening.py", "app")

    assert boundary is not None and midword is not None
    assert boundary.score > midword.score


def test_shorter_path_wins_on_equal_match() -> None:
    short = score_path("app.py", "app")
    long = score_path("a/very/deeply/nested/path/app.py", "app")

    assert short is not None and long is not None
    assert short.score > long.score


def test_smart_case_lowercase_query_is_insensitive() -> None:
    assert score_path("src/README.md", "readme") is not None


def test_smart_case_uppercase_query_demands_exact_case() -> None:
    assert score_path("src/readme.md", "README") is None
    assert score_path("src/README.md", "README") is not None


def test_offsets_mark_matched_positions() -> None:
    result = score_path("app.py", "app")

    assert result is not None
    assert result.offsets == (0, 1, 2)


# --- filtering -------------------------------------------------------------


def test_empty_query_returns_corpus_head() -> None:
    corpus = tuple(f"file_{index}.py" for index in range(50))

    results = filter_paths(corpus, "", limit=5)

    assert tuple(result.path for result in results) == corpus[:5]


def test_filter_ranks_best_match_first() -> None:
    corpus = ("a/p/p/other.py", "src/textual_app.py", "app.py")

    results = filter_paths(corpus, "app")

    assert results[0].path == "app.py"


def test_filter_drops_non_matches() -> None:
    corpus = ("app.py", "unrelated.txt")

    results = filter_paths(corpus, "app")

    assert tuple(result.path for result in results) == ("app.py",)


def test_filter_respects_limit() -> None:
    corpus = tuple(f"app_{index}.py" for index in range(40))

    assert len(filter_paths(corpus, "app", limit=10)) == 10


def test_filter_ordering_is_deterministic_on_ties() -> None:
    """Equal scores must break on (length, path) or pilot assertions flake."""

    corpus = ("b_app.py", "a_app.py")

    first = tuple(result.path for result in filter_paths(corpus, "app"))
    second = tuple(result.path for result in filter_paths(corpus[::-1], "app"))

    assert first == second
