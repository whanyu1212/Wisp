"""Tests for the ``@``-picker's path collection and fuzzy ranking."""

from __future__ import annotations

from pathlib import Path

import pytest

from wisp.tools.context import ToolContext
from wisp.tui.file_index import (
    FileIndexConfig,
    FileIndexRequest,
    ProjectDirectory,
    ProjectFile,
    collect_paths,
    collect_project_snapshot,
    filter_paths,
    format_file_reference,
    parse_file_reference,
    score_path,
)
from wisp.tui.textual_app import _build_file_index_snapshot, _file_index_context

pytestmark = pytest.mark.tui


def _config(root: Path, **overrides: object) -> FileIndexConfig:
    return FileIndexConfig(root=root, context=ToolContext(cwd=root), **overrides)  # type: ignore[arg-type]


def _write(root: Path, relative: str, content: str = "x") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# --- collection ------------------------------------------------------------


def test_collects_typed_hierarchy_and_projects_legacy_paths(tmp_path: Path) -> None:
    _write(tmp_path, "src/wisp/app.py")
    _write(tmp_path, "README.md")

    snapshot = collect_project_snapshot(_config(tmp_path))

    assert ProjectFile("README.md") in snapshot.entries
    assert ProjectDirectory("src") in snapshot.entries
    assert ProjectDirectory("src/wisp") in snapshot.entries
    assert ProjectFile("src/wisp/app.py") in snapshot.entries
    assert snapshot.children_of() == ("README.md", "src")
    assert snapshot.children_of("src/") == ("src/wisp",)
    assert snapshot.children_of("src/wisp") == ("src/wisp/app.py",)
    assert snapshot.paths == collect_paths(_config(tmp_path))
    assert "src/" in snapshot.paths


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


def test_respects_entry_cap_and_reports_truncation(tmp_path: Path) -> None:
    for index in range(50):
        _write(tmp_path, f"file_{index:03d}.txt")

    snapshot = collect_project_snapshot(_config(tmp_path, max_entries=10))

    assert len(snapshot.entries) == 10
    assert snapshot.truncation.entry_limit_reached is True
    assert snapshot.truncated is True


def test_respects_depth_cap_and_reports_truncation(tmp_path: Path) -> None:
    _write(tmp_path, "a/b/c/d/deep.txt")

    snapshot = collect_project_snapshot(_config(tmp_path, max_depth=2))

    assert not any(path.startswith("a/b/c") for path in snapshot.paths)
    assert "a/" in snapshot.paths
    assert snapshot.truncation.depth_limit_reached is True


def test_queued_directory_replaced_by_symlink_cannot_disclose_outside_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued = tmp_path / "queued"
    _write(queued, "inside.txt")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    _write(outside, "outside-secret-name.txt")
    original = tmp_path / "queued-original"
    real_scandir = __import__("os").scandir
    replaced = False

    def racing_scandir(path: object):  # type: ignore[no-untyped-def]
        nonlocal replaced
        if Path(path) == queued and not replaced:
            queued.rename(original)
            queued.symlink_to(outside, target_is_directory=True)
            replaced = True
        return real_scandir(path)  # type: ignore[arg-type]

    monkeypatch.setattr("wisp.tui.file_index.os.scandir", racing_scandir)
    try:
        snapshot = collect_project_snapshot(_config(tmp_path))
    finally:
        if queued.is_symlink():
            queued.unlink()
        if original.exists():
            original.rename(queued)

    assert replaced is True
    assert "outside-secret-name.txt" not in snapshot.paths
    assert "queued/outside-secret-name.txt" not in snapshot.paths


def test_omits_file_directory_and_dangling_symlinks(tmp_path: Path) -> None:
    """No symlink type is mentionable, and directory links are never followed."""

    target = _write(tmp_path, "real/file.txt")
    (tmp_path / "file-link").symlink_to(target)
    (tmp_path / "dir-link").symlink_to(tmp_path / "real", target_is_directory=True)
    (tmp_path / "dangling-link").symlink_to(tmp_path / "missing")

    paths = collect_paths(_config(tmp_path))

    assert "real/file.txt" in paths
    assert not any("link" in path for path in paths)


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


def test_context_prefers_the_caller_resolved_policy(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A resolved policy must be used verbatim, never re-derived from the environment.

    Only the parent knows about an ``--auth-file`` override or a trusted project's
    in-project ``auth_path``; re-resolving here would drop both and leave the real
    credential file mentionable.
    """

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("resolved policy supplied; must not re-resolve config")

    monkeypatch.setattr("wisp.tui.textual_app.WispConfig.from_env", _boom)
    _write(tmp_path, "creds.json", "{}")
    _write(tmp_path, "app.py")

    context = _file_index_context(tmp_path, (".env", "creds.json"))
    paths = collect_paths(FileIndexConfig(root=tmp_path, context=context))

    assert context.protected_paths == (".env", "creds.json")
    assert "creds.json" not in paths
    assert "app.py" in paths


def test_run_tui_forwards_the_resolved_auth_path_to_the_picker(tmp_path: Path) -> None:
    """The nonstandard credential file the parent resolved must reach the picker."""

    from wisp.config import WispConfig

    auth_path = tmp_path / "custom-auth.json"
    config = WispConfig(auth_path=auth_path)
    # Mirrors what run_tui hands create_textual_tui.
    protected_paths = ToolContext.from_config(config).protected_paths

    context = _file_index_context(tmp_path, protected_paths)

    assert any(pattern.endswith("custom-auth.json") for pattern in context.protected_paths)


def test_adopted_auth_path_is_added_to_a_supplied_policy(tmp_path: Path) -> None:
    """A credential file adopted mid-session must join the startup policy."""

    _write(tmp_path, "project-auth.json", "{}")
    _write(tmp_path, "app.py")
    adopted = (tmp_path / "project-auth.json").resolve().as_posix()

    context = _file_index_context(tmp_path, (".env",), (adopted,))
    paths = collect_paths(FileIndexConfig(root=tmp_path, context=context))

    assert ".env" in context.protected_paths
    assert "project-auth.json" not in paths
    assert "app.py" in paths


def test_adopted_auth_path_applies_to_the_fallback_policy(tmp_path: Path) -> None:
    """Deferred trust protects the new credential however the base policy was found."""

    _write(tmp_path, "project-auth.json", "{}")
    adopted = (tmp_path / "project-auth.json").resolve().as_posix()

    context = _file_index_context(tmp_path, None, (adopted,))
    paths = collect_paths(FileIndexConfig(root=tmp_path, context=context))

    assert ".env" in context.protected_paths  # base policy survives
    assert "project-auth.json" not in paths


def test_context_falls_back_to_secure_defaults(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Config resolution failing must not crash the TUI, and must not open the gate."""

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr("wisp.tui.textual_app.WispConfig.from_env", _boom)

    context = _file_index_context(tmp_path)

    assert ".env" in context.protected_paths


def test_worker_snapshot_build_fails_closed_when_root_canonicalization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = FileIndexRequest(generation=1, cwd="/unresolvable/project")

    def fail_resolve(*_args: object, **_kwargs: object) -> Path:
        raise OSError("canonicalization failed")

    monkeypatch.setattr(Path, "resolve", fail_resolve)

    assert _build_file_index_snapshot(request) is None


# --- formatting ------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/app.py", "@src/app.py"),
        ("my notes.md", '@"my notes.md"'),
        ('say "hi".txt', '@"say \\"hi\\".txt"'),
        ("back\\slash.txt", '@"back\\\\slash.txt"'),
        ("control\x00name", '@"control\\u0000name"'),
        ("资料/说明.md", "@资料/说明.md"),
        ("资料/项目 说明.md", '@"资料/项目 说明.md"'),
    ],
)
def test_formats_file_reference_with_json_quoting(path: str, expected: str) -> None:
    assert format_file_reference(path) == expected
    assert parse_file_reference(expected, start=0) == (len(expected), path)


def test_quoted_reference_cut_at_bound_does_not_validate_attached_suffix() -> None:
    text = '@"known.py"suffix'
    bound = len('@"known.py"')

    assert parse_file_reference(text, start=0, limit=bound) is None


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
