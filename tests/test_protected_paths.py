"""Tests for protected-secret-path enforcement in tools."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from tests.test_tools import run_tool
from wisp.settings import DEFAULT_PROTECTED_PATHS
from wisp.tools import search as search_tools_module
from wisp.tools.builtin import FindTool, GrepTool, ReadTool
from wisp.tools.context import ToolContext
from wisp.tools.paths import is_protected_path, resolve_tool_path
from wisp.tools.result import ToolError

pytestmark = pytest.mark.process

PROTECTED = (".env", "*.key", "credentials.json", ".wisp/auth.json", ".wisp/settings.json")


def _context(cwd: Path, **kwargs: object) -> ToolContext:
    return ToolContext(cwd=cwd, protected_paths=PROTECTED, **kwargs)  # type: ignore[arg-type]


# --- is_protected_path unit behavior ---


@pytest.mark.parametrize(
    "relative",
    [
        ".env",
        "nested/dir/.env",
        "secrets.key",
        "sub/api.key",
        "config/credentials.json",
        ".wisp/auth.json",
        ".wisp/settings.json",
    ],
)
def test_protected_patterns_match(tmp_path: Path, relative: str) -> None:
    target = tmp_path / relative
    assert is_protected_path(target, _context(tmp_path)) is True


@pytest.mark.parametrize(
    "relative",
    [
        "main.py",
        "env.txt",  # not ".env"
        "notes/env",
        "keychain.py",  # not "*.key"
        "credentials.json.bak",
    ],
)
def test_unprotected_patterns_do_not_match(tmp_path: Path, relative: str) -> None:
    target = tmp_path / relative
    assert is_protected_path(target, _context(tmp_path)) is False


def test_no_patterns_means_nothing_protected(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path, protected_paths=())
    assert is_protected_path(tmp_path / ".env", context) is False


def test_matching_is_case_insensitive(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path, protected_paths=(".env",))
    assert is_protected_path(tmp_path / ".ENV", context) is True


# --- resolve_tool_path enforcement ---


def test_resolve_tool_path_rejects_protected(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    with pytest.raises(ToolError, match="protected path"):
        resolve_tool_path(".env", _context(tmp_path))


def test_resolve_tool_path_allows_ordinary_file(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    resolved = resolve_tool_path("main.py", _context(tmp_path))
    assert resolved == (tmp_path / "main.py").resolve()


# --- end-to-end tool behavior ---


def test_read_tool_denies_protected_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")
    with pytest.raises(ToolError, match="protected path"):
        run_tool(ReadTool(), {"path": ".env"}, _context(tmp_path))


def test_read_tool_allows_ordinary_file(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    result = run_tool(ReadTool(), {"path": "main.py"}, _context(tmp_path))
    assert "print('hi')" in result.text


@pytest.mark.parametrize("force_python", [False, True])
def test_grep_skips_protected_files(
    tmp_path: Path, monkeypatch: MonkeyPatch, force_python: bool
) -> None:
    # Cover both the rg-backed path and the pure-Python fallback: the secret must
    # never appear in grep output regardless of which engine runs.
    if force_python:
        monkeypatch.setattr(search_tools_module.shutil, "which", lambda _name: None)

    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-needle\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("token = 'sk-needle'\n", encoding="utf-8")

    result = run_tool(GrepTool(), {"pattern": "sk-needle"}, _context(tmp_path))

    assert "app.py" in result.text
    assert ".env" not in result.text
    assert "OPENAI_API_KEY" not in result.text


@pytest.mark.parametrize("force_python", [False, True])
def test_find_skips_protected_files(
    tmp_path: Path, monkeypatch: MonkeyPatch, force_python: bool
) -> None:
    if force_python:
        monkeypatch.setattr(search_tools_module.shutil, "which", lambda _name: None)

    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (tmp_path / "keep.env.py").write_text("ok\n", encoding="utf-8")

    result = run_tool(FindTool(), {"pattern": "*"}, _context(tmp_path))

    assert ".env" not in result.text.split()  # the bare .env file is hidden
    assert "keep.env.py" in result.text


def test_find_pointed_directly_at_protected_file_is_denied(tmp_path: Path) -> None:
    # Pointing find/grep AT a protected file (not just walking past it) must be
    # denied too. The path argument resolves through resolve_tool_path first, so
    # the guard fires before the "candidates = [path]" direct-file branch runs.
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    with pytest.raises(ToolError, match="protected path"):
        run_tool(FindTool(), {"pattern": "*", "path": ".env"}, _context(tmp_path))


def test_grep_pointed_directly_at_protected_file_is_denied(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")
    with pytest.raises(ToolError, match="protected path"):
        run_tool(GrepTool(), {"pattern": "sk-secret", "path": ".env"}, _context(tmp_path))


# --- Regression tests for Codex review findings (commit review of #50 PR 1) ---


def test_grep_user_glob_cannot_reinclude_protected_file(tmp_path: Path) -> None:
    # Finding 1: rg's --glob exclusions are last-match-wins, so a caller-supplied
    # glob could re-include a secret. The output post-filter must still hide it.
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-needle\n", encoding="utf-8")

    result = run_tool(
        GrepTool(),
        {"pattern": "sk-needle", "path": ".", "glob": ".env", "literal": True},
        _context(tmp_path),
    )

    assert "sk-needle" not in result.text
    assert result.data["count"] == 0


def test_grep_protects_case_variant_of_key_pattern(tmp_path: Path) -> None:
    # Finding 2: rg globs are case-sensitive but is_protected_path is not; an
    # uppercase API.KEY must not leak via the rg engine.
    (tmp_path / "API.KEY").write_text("PRIVATE=sk-needle\n", encoding="utf-8")

    result = run_tool(
        GrepTool(), {"pattern": "sk-needle", "path": ".", "literal": True}, _context(tmp_path)
    )

    assert "sk-needle" not in result.text
    assert "API.KEY" not in result.text


def test_protected_path_suffix_matches_outside_cwd() -> None:
    # Finding 3: with allow_outside_cwd, a slash-bearing default like
    # .wisp/auth.json must still match an absolute path read from elsewhere.
    context = ToolContext(
        cwd=Path("/repo"), allow_outside_cwd=True, protected_paths=DEFAULT_PROTECTED_PATHS
    )

    assert is_protected_path(Path("/home/user/.wisp/auth.json"), context) is True
    assert is_protected_path(Path("/home/user/.wisp/settings.json"), context) is True
    assert is_protected_path(Path("/tmp/elsewhere/.env"), context) is True


def test_default_context_blocks_settings_file_with_mcp_secrets(tmp_path: Path) -> None:
    settings_dir = tmp_path / ".wisp"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        '{"mcp_servers":{"server":{"command":"server","env":{"TOKEN":"secret"}}}}',
        encoding="utf-8",
    )

    with pytest.raises(ToolError, match="protected path"):
        run_tool(ReadTool(), {"path": ".wisp/settings.json"}, ToolContext(cwd=tmp_path))


def test_default_tool_context_is_secure_by_default(tmp_path: Path) -> None:
    # Finding 4: a bare ToolContext (as embedding/SDK code might build) must carry
    # the default protections without an explicit opt-in.
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    assert context.protected_paths == DEFAULT_PROTECTED_PATHS
    with pytest.raises(ToolError, match="protected path"):
        run_tool(ReadTool(), {"path": ".env"}, context)


def test_explicit_empty_protected_paths_opts_out(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=readable\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, protected_paths=())

    result = run_tool(ReadTool(), {"path": ".env"}, context)
    assert "readable" in result.text


def test_default_env_example_is_not_protected(tmp_path: Path) -> None:
    # Finding 5: the shipped defaults must not block committed placeholder files.
    (tmp_path / ".env.example").write_text("TOKEN=changeme\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)  # uses DEFAULT_PROTECTED_PATHS

    result = run_tool(ReadTool(), {"path": ".env.example"}, context)
    assert "changeme" in result.text


def test_default_bare_pattern_does_not_match_directory_component() -> None:
    # Finding 5: bare patterns match a filename, not an arbitrary path component.
    context = ToolContext(cwd=Path("/repo"))  # uses DEFAULT_PROTECTED_PATHS

    assert is_protected_path(Path("/repo/docs/id_rsa/README.md"), context) is False
    assert is_protected_path(Path("/repo/id_rsa"), context) is True


# --- Regression tests for the second Codex review pass (fixes to the fixes) ---


def test_grep_record_parser_fails_closed_on_separator_in_filename() -> None:
    # Re-review finding 1: rg's \x1f field separator can appear inside a filename,
    # making the record ambiguous. Such a record must fail closed (be dropped),
    # never mis-parsed to a wrong, non-protected path that leaks the secret.
    from wisp.tools.search import RG_MATCH_SEPARATOR as SEP
    from wisp.tools.search import _rg_grep_line_is_protected

    context = ToolContext(cwd=Path("/repo"), protected_paths=("id_rsa",))
    leaky = f"dir{SEP}123{SEP}/id_rsa{SEP}1{SEP}SECRET=leak"

    assert _rg_grep_line_is_protected(leaky, context) is True


def test_grep_record_parser_fails_closed_on_truncated_fragment() -> None:
    # Re-review finding 4: a record truncated by output buffering (missing the
    # text field) can't be parsed to a path and must fail closed.
    from wisp.tools.search import RG_MATCH_SEPARATOR as SEP
    from wisp.tools.search import _rg_grep_line_is_protected

    context = ToolContext(cwd=Path("/repo"), protected_paths=("id_rsa",))
    fragment = f"id_rsa{SEP}1"  # truncated: no text field

    assert _rg_grep_line_is_protected(fragment, context) is True


def test_grep_record_parser_keeps_non_file_lines() -> None:
    from wisp.tools.search import _rg_grep_line_is_protected

    context = ToolContext(cwd=Path("/repo"), protected_paths=("id_rsa",))

    assert _rg_grep_line_is_protected("--", context) is False
    assert _rg_grep_line_is_protected("prose with no separators", context) is False


def test_rg_find_protected_files_do_not_exhaust_result_budget(tmp_path: Path) -> None:
    # Re-review finding 2: protected candidates must be filtered at the subprocess
    # boundary so they don't consume the max_results line budget, which would hide
    # a legitimate reportable file behind a run of protected ones.
    (tmp_path / "0.key").write_text("x\n", encoding="utf-8")
    (tmp_path / "1.key").write_text("x\n", encoding="utf-8")
    (tmp_path / "z.py").write_text("x\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, protected_paths=("*.key",))

    result = run_tool(FindTool(), {"pattern": "*", "path": ".", "max_results": 1}, context)

    assert "z.py" in result.text
    assert ".key" not in result.text


@pytest.mark.parametrize("variant", [".env.prod", ".env.dev", ".env.qa", ".env.secrets"])
def test_default_list_covers_common_env_secret_variants(tmp_path: Path, variant: str) -> None:
    # Re-review finding 5: the default list must catch common real-secret env
    # variants, not only the fully spelled-out forms.
    context = ToolContext(cwd=tmp_path)  # DEFAULT_PROTECTED_PATHS
    assert is_protected_path(tmp_path / variant, context) is True


def test_dropping_protected_groups_leaves_no_orphan_separator() -> None:
    # Third-review finding: dropping a protected context group must not leave a
    # dangling "--" separator (which would render as output with count == 0).
    from wisp.tools.search import RG_MATCH_SEPARATOR as SEP
    from wisp.tools.search import _result_from_grep_lines

    context = ToolContext(cwd=Path("/repo"), protected_paths=("id_rsa",))

    # A protected match fenced by rg group separators collapses to a clean miss.
    only_protected = _result_from_grep_lines(
        ["--", f"id_rsa{SEP}1{SEP}SECRET", "--"], max_results=10, context=context
    )
    assert only_protected.text == "No matches"
    assert only_protected.data["count"] == 0

    # A legitimate group followed by a dropped protected group keeps the real hit
    # without a trailing orphan separator.
    mixed = _result_from_grep_lines(
        [f"app.py{SEP}1{SEP}hit", "--", f"id_rsa{SEP}2{SEP}SECRET"],
        max_results=10,
        context=context,
    )
    assert mixed.text == "app.py:1:hit"
    assert mixed.data["count"] == 1


def test_separator_between_two_kept_groups_is_preserved() -> None:
    # The orphan-separator cleanup must not strip separators between real groups.
    from wisp.tools.search import RG_MATCH_SEPARATOR as SEP
    from wisp.tools.search import _result_from_grep_lines

    context = ToolContext(cwd=Path("/repo"), protected_paths=("id_rsa",))
    result = _result_from_grep_lines(
        [f"app.py{SEP}1{SEP}hit1", "--", f"app.py{SEP}5{SEP}hit2"],
        max_results=10,
        context=context,
    )

    assert result.text == "app.py:1:hit1\n--\napp.py:5:hit2"
    assert result.data["count"] == 2


def test_protected_matches_do_not_starve_a_later_real_match(tmp_path: Path) -> None:
    # PR #58 review P2: protected records must be dropped BEFORE buffering. A large
    # set of protected matches preceding an ordinary match must not exhaust the
    # stdout buffer and kill rg before the real match is read. Only the rg engine
    # buffers a single stream, so this scenario is rg-specific.
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    big_secret = "NEEDLE_SECRET=" + ("x" * 400) + "\n"
    for i in range(30):
        directory = tmp_path / f"aaa{i:02d}"
        directory.mkdir()
        (directory / ".env").write_text(big_secret, encoding="utf-8")
    (tmp_path / "zzz.py").write_text("found_NEEDLE_here\n", encoding="utf-8")

    # A small stdout buffer: the protected bytes would overflow it if buffered.
    context = ToolContext(
        cwd=tmp_path,
        protected_paths=(".env",),
        max_output_bytes=2000,
        max_output_lines=100,
    )

    result = run_tool(GrepTool(), {"pattern": "NEEDLE", "path": "."}, context)

    assert "zzz.py" in result.text  # the real match survives
    assert "NEEDLE_SECRET" not in result.text  # no secret leaked
    assert "aaa" not in result.text


def test_protected_group_separators_do_not_starve_context_grep(tmp_path: Path) -> None:
    # PR #58 review (5907c8b): with context > 0, rg emits bare "--" group
    # separators. Dropping protected records but keeping their separators could let
    # a large run of protected groups exhaust the line buffer and lose a later
    # match. Separators around dropped groups must be suppressed before buffering.
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    # Case-variant .KEY names: is_protected_path (case-insensitive) protects them,
    # but rg's case-sensitive glob misses them, so rg emits records + "--".
    for i in range(50):
        (tmp_path / f"aaa{i:02d}.KEY").write_text("pre\nNEEDLE_SECRET=x\npost\n", encoding="utf-8")
    (tmp_path / "zzz.py").write_text("found_NEEDLE\n", encoding="utf-8")

    # Tiny line budget: 50 protected groups' separators alone would overflow it.
    context = ToolContext(
        cwd=tmp_path,
        protected_paths=("*.key",),
        max_output_lines=10,
        max_output_bytes=100_000,
    )

    result = run_tool(GrepTool(), {"pattern": "NEEDLE", "path": ".", "context": 1}, context)

    assert "zzz.py" in result.text  # the real match survives the separator flood
    assert "NEEDLE_SECRET" not in result.text  # no secret leaked
    assert "aaa" not in result.text


def test_context_grep_preserves_separators_between_kept_groups(tmp_path: Path) -> None:
    # The separator suppression must not eat legitimate separators between two kept
    # context groups.
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    (tmp_path / "file1.py").write_text("a\nNEEDLE_one\nb\n", encoding="utf-8")
    (tmp_path / "file2.py").write_text("c\nNEEDLE_two\nd\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, protected_paths=(".env",))

    result = run_tool(GrepTool(), {"pattern": "NEEDLE", "path": ".", "context": 1}, context)

    assert result.data["count"] == 2
    assert "--" in result.text  # separator between the two real groups is kept
    assert "NEEDLE_one" in result.text
    assert "NEEDLE_two" in result.text


# --- Re-review round 2 (PR #58 commit 9696c36): symlink + custom auth file ---


def test_protected_symlink_name_is_denied_before_dereferencing(tmp_path: Path) -> None:
    # P1: a protected NAME that is a symlink to an innocuous target must be denied
    # by its requested name, not silently allowed via the resolved target.
    (tmp_path / "actualdata").write_text("API_KEY=sk-secret\n", encoding="utf-8")
    (tmp_path / ".env").symlink_to(tmp_path / "actualdata")
    context = _context(tmp_path)

    with pytest.raises(ToolError, match="protected path"):
        run_tool(ReadTool(), {"path": ".env"}, context)


def test_symlink_pointing_at_secret_target_is_also_denied(tmp_path: Path) -> None:
    # The other direction: an innocuously named symlink pointing AT a protected
    # target is caught via the resolved path.
    (tmp_path / "hidden.key").write_text("k\n", encoding="utf-8")
    (tmp_path / "notes.txt").symlink_to(tmp_path / "hidden.key")
    context = _context(tmp_path)

    assert is_protected_path(tmp_path / "notes.txt", context) is True


def test_grep_skips_protected_symlink(tmp_path: Path) -> None:
    (tmp_path / "actualdata").write_text("API_KEY=sk-needle\n", encoding="utf-8")
    (tmp_path / ".env").symlink_to(tmp_path / "actualdata")
    context = ToolContext(cwd=tmp_path, protected_paths=(".env",))

    result = run_tool(GrepTool(), {"pattern": "sk-needle", "path": "."}, context)

    # The .env symlink is skipped; the real (unprotected) file is fine to surface.
    assert ".env" not in result.text


def test_configured_auth_file_is_protected(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    # P2: a custom credential file (via WISP_AUTH_FILE) is Wisp's active secret and
    # must be protected even though it isn't named like the default auth.json.
    from wisp.config import WispConfig

    auth_file = tmp_path / "codex-auth.json"
    auth_file.write_text('{"token": "sk-super-secret"}\n', encoding="utf-8")
    monkeypatch.setenv("WISP_AUTH_FILE", str(auth_file))

    config = WispConfig.from_env()
    context = ToolContext.from_config(config, cwd=tmp_path)

    assert any("codex-auth.json" in pattern for pattern in config.protected_paths)
    with pytest.raises(ToolError, match="protected path"):
        run_tool(ReadTool(), {"path": "codex-auth.json"}, context)


def test_auth_file_protected_even_when_guard_disabled(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # Disabling the general guard must not expose Wisp's own credential file.
    from wisp.config import WispConfig

    auth_file = tmp_path / "codex-auth.json"
    auth_file.write_text('{"token": "sk-secret"}\n', encoding="utf-8")
    monkeypatch.setenv("WISP_AUTH_FILE", str(auth_file))
    (tmp_path / ".wisp").mkdir()
    (tmp_path / ".wisp" / "settings.json").write_text('{"protected_paths": []}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    config = WispConfig.from_env()
    context = ToolContext.from_config(config, cwd=tmp_path)

    with pytest.raises(ToolError, match="protected path"):
        run_tool(ReadTool(), {"path": "codex-auth.json"}, context)


def test_directly_constructed_config_protects_auth_file(tmp_path: Path) -> None:
    # Re-review: building WispConfig directly (embedding/SDK), bypassing from_env,
    # must still protect the credential file. Enforced as a model invariant.
    from wisp.config import WispConfig

    auth_file = tmp_path / "codex-auth.json"
    auth_file.write_text('{"token": "sk-super-secret"}\n', encoding="utf-8")

    config = WispConfig(auth_path=auth_file)
    context = ToolContext.from_config(config, cwd=tmp_path)

    assert any("codex-auth.json" in pattern for pattern in config.protected_paths)
    with pytest.raises(ToolError, match="protected path"):
        run_tool(ReadTool(), {"path": "codex-auth.json"}, context)


def test_directly_constructed_config_protects_auth_even_with_empty_guard(
    tmp_path: Path,
) -> None:
    from wisp.config import WispConfig

    auth_file = tmp_path / "codex-auth.json"
    auth_file.write_text('{"token": "sk-secret"}\n', encoding="utf-8")

    config = WispConfig(auth_path=auth_file, protected_paths=())
    context = ToolContext.from_config(config, cwd=tmp_path)

    with pytest.raises(ToolError, match="protected path"):
        run_tool(ReadTool(), {"path": "codex-auth.json"}, context)


def test_from_config_backstops_auth_protection_after_model_copy(tmp_path: Path) -> None:
    # model_copy skips validators; ToolContext.from_config must still protect the
    # (new) auth file so a validation-skipping copy can't expose the credential.
    from wisp.config import WispConfig

    auth_file = tmp_path / "codex-auth.json"
    auth_file.write_text('{"token": "sk-secret"}\n', encoding="utf-8")

    config = WispConfig().model_copy(update={"auth_path": auth_file})
    context = ToolContext.from_config(config, cwd=tmp_path)

    with pytest.raises(ToolError, match="protected path"):
        run_tool(ReadTool(), {"path": "codex-auth.json"}, context)


def test_from_config_backstops_settings_protection_after_model_copy(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    from wisp.config import WispConfig

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    settings_dir = tmp_path / ".wisp"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        '{"mcp_servers":{"server":{"command":"server","env":{"TOKEN":"secret"}}}}',
        encoding="utf-8",
    )
    config = WispConfig().model_copy(update={"protected_paths": ()})

    context = ToolContext.from_config(config, cwd=tmp_path)

    with pytest.raises(ToolError, match="protected path"):
        run_tool(ReadTool(), {"path": ".wisp/settings.json"}, context)


def test_project_settings_cannot_disable_secret_guard(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # Security (review finding 3): a project ./.wisp/settings.json shipping
    # {"protected_paths": []} must NOT disable the guard end to end. read(.env)
    # stays blocked because project protected_paths are ignored.
    import json

    from wisp.config import WispConfig

    wisp_dir = tmp_path / ".wisp"
    wisp_dir.mkdir()
    (wisp_dir / "settings.json").write_text(json.dumps({"protected_paths": []}), encoding="utf-8")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    config = WispConfig.from_env()
    context = ToolContext.from_config(config, cwd=tmp_path)

    with pytest.raises(ToolError, match="protected path"):
        run_tool(ReadTool(), {"path": ".env"}, context)
