from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch

from wisp.agent import prompt as prompt_module
from wisp.agent.prompt import (
    build_project_context,
    build_prompt_messages,
    build_untrusted_project_context,
)
from wisp.providers.base import ToolSpec


def test_build_prompt_messages_includes_default_instructions_and_context(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    tool = ToolSpec(
        name="read",
        description="Read a UTF-8 text file.",
        input_schema={"type": "object", "properties": {}},
    )

    messages = build_prompt_messages(cwd=tmp_path, tools=[tool])

    assert [message.role for message in messages] == ["system", "system"]
    assert "You are Wisp" in messages[0].content
    assert "Operate like a careful software engineering assistant" in messages[0].content
    assert f"cwd: {tmp_path.resolve(strict=False)}" in messages[1].content
    assert "project files:\n  pyproject.toml" in messages[1].content
    assert "allowed tools:\n  - read: Read a UTF-8 text file." in messages[1].content


def test_build_prompt_messages_can_skip_project_context(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("Do project-specific things.\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("Legacy Claude guidance.\n", encoding="utf-8")
    tool = ToolSpec(
        name="read",
        description="Read a UTF-8 text file.",
        input_schema={"type": "object", "properties": {}},
    )

    messages = build_prompt_messages(
        cwd=tmp_path,
        tools=[tool],
        include_project_context=False,
    )

    context = messages[1].content
    assert str(tmp_path.resolve(strict=False)) not in context
    assert "pyproject.toml" not in context
    assert "AGENTS.md" not in context
    assert "CLAUDE.md" not in context
    assert "Do project-specific things." not in context
    assert "Legacy Claude guidance." not in context
    assert "git:" not in context
    assert "project context: skipped because this project is not trusted" in context
    assert "allowed tools:\n  - read: Read a UTF-8 text file." in context


def test_project_context_reports_no_allowed_tools(tmp_path: Path) -> None:
    context = build_project_context(cwd=tmp_path, tools=[])

    assert "project instructions:" not in context
    assert "allowed tools: none exposed to the model" in context


def test_project_context_uses_first_root_context_file_by_pi_precedence(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("Prefer small typed Python modules.\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("Legacy Claude-compatible notes.\n", encoding="utf-8")

    context = build_project_context(cwd=tmp_path)

    assert "project instructions:" in context
    assert "--- AGENTS.md ---\nPrefer small typed Python modules." in context
    assert "--- CLAUDE.md ---" not in context
    assert "Legacy Claude-compatible notes." not in context


def test_project_context_falls_back_to_claude_context_file(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("Claude-compatible notes.\n", encoding="utf-8")

    context = build_project_context(cwd=tmp_path)

    assert "--- CLAUDE.md ---\nClaude-compatible notes." in context


def test_project_context_supports_uppercase_context_file_names(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "AGENTS.MD").write_text("Uppercase agent notes.\n", encoding="utf-8")

    context = build_project_context(cwd=tmp_path)

    assert "Uppercase agent notes." in context
    assert "--- AGENTS.md ---" in context or "--- AGENTS.MD ---" in context


def test_project_context_includes_nested_context_files_root_to_cwd(tmp_path: Path) -> None:
    project = tmp_path / "project"
    subdir = project / "packages" / "app"
    subdir.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (project / "AGENTS.md").write_text("Root agent rules.\n", encoding="utf-8")
    (project / "CLAUDE.md").write_text("Root Claude rules.\n", encoding="utf-8")
    (subdir / "AGENTS.md").write_text("App agent rules.\n", encoding="utf-8")
    (subdir / "CLAUDE.md").write_text("App Claude rules.\n", encoding="utf-8")

    context = build_project_context(cwd=subdir, trusted_context_root=project)

    expected_order = [
        "--- AGENTS.md ---",
        "Root agent rules.",
        "--- packages/app/AGENTS.md ---",
        "App agent rules.",
    ]
    positions = [context.index(item) for item in expected_order]
    assert positions == sorted(positions)
    assert "Root Claude rules." not in context
    assert "App Claude rules." not in context


def test_project_context_defaults_to_trusted_cwd_for_context_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    subdir = project / "packages" / "app"
    subdir.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (project / "AGENTS.md").write_text("Parent agent rules.\n", encoding="utf-8")
    (subdir / "AGENTS.md").write_text("Trusted cwd rules.\n", encoding="utf-8")

    context = build_project_context(cwd=subdir)

    assert "Parent agent rules." not in context
    assert "--- packages/app/AGENTS.md ---\nTrusted cwd rules." in context


def test_project_context_uses_context_file_as_root_marker(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    project = tmp_path / "project"
    subdir = project / "src"
    subdir.mkdir(parents=True)
    (project / "AGENTS.md").write_text("Root-only agent guidance.\n", encoding="utf-8")
    monkeypatch.setattr(prompt_module, "_run_git", lambda _cwd, *args: None)

    context = build_project_context(cwd=subdir)

    assert f"project root: {project.resolve(strict=False)}" in context
    assert "Root-only agent guidance." not in context


def test_project_context_file_budget_truncates_only_instructions(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("A" * 200, encoding="utf-8")
    tool = ToolSpec(
        name="read",
        description="Read a UTF-8 text file.",
        input_schema={"type": "object", "properties": {}},
    )

    context = build_project_context(cwd=tmp_path, tools=[tool], max_context_file_chars=80)

    assert "project instructions:" in context
    assert "[context truncated]" in context
    assert "project files:\n  pyproject.toml" in context
    assert "allowed tools:\n  - read: Read a UTF-8 text file." in context


def test_project_context_skips_symlink_context_file(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=leak\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").symlink_to(tmp_path / ".env")
    (tmp_path / "CLAUDE.md").write_text("fallback instructions\n", encoding="utf-8")

    context = build_project_context(cwd=tmp_path)

    assert "SECRET=leak" not in context
    assert "--- CLAUDE.md ---\nfallback instructions" in context


def test_project_context_skips_protected_context_file(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("SECRET=leak\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("safe fallback\n", encoding="utf-8")

    context = build_project_context(cwd=tmp_path, protected_paths=("AGENTS.md",))

    assert "SECRET=leak" not in context
    assert "--- CLAUDE.md ---\nsafe fallback" in context


def test_long_project_context_file_cannot_hide_allowed_tools(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("A" * 5_000, encoding="utf-8")
    tool = ToolSpec(
        name="read",
        description="Read a UTF-8 text file.",
        input_schema={"type": "object", "properties": {}},
    )

    context = build_project_context(cwd=tmp_path, tools=[tool], max_chars=1_200)

    assert len(context) <= 1_200
    assert "allowed tools:\n  - read: Read a UTF-8 text file." in context
    assert "project instructions:" in context
    assert "[context truncated]" in context
    assert context.index("allowed tools:") < context.index("project instructions:")


def test_untrusted_project_context_reports_tools_without_local_context(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    tool = ToolSpec(
        name="grep",
        description="Search text files.",
        input_schema={"type": "object", "properties": {}},
    )

    context = build_untrusted_project_context(tools=[tool])

    assert str(tmp_path.resolve(strict=False)) not in context
    assert "README.md" not in context
    assert "git:" not in context
    assert "allowed tools:\n  - grep: Search text files." in context


def test_project_context_scans_git_root_from_subdirectory(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    subdir = repo / "src" / "wisp"
    subdir.mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")

    def fake_run_git(_cwd: Path, *args: str) -> str | None:
        if args == ("rev-parse", "--show-toplevel"):
            return str(repo)
        if args == ("rev-parse", "--is-inside-work-tree"):
            return "true"
        if args == ("branch", "--show-current"):
            return "main"
        if args == ("status", "--short"):
            return ""
        return None

    monkeypatch.setattr(prompt_module, "_run_git", fake_run_git)

    context = build_project_context(cwd=subdir)

    assert f"cwd: {subdir.resolve(strict=False)}" in context
    assert f"project root: {repo.resolve(strict=False)}" in context
    assert "git: branch main; status clean" in context
    assert "project files:\n  pyproject.toml" in context
    assert "  README.md" in context
    assert "project files: none detected" not in context


def test_project_context_walks_parents_without_git(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    project = tmp_path / "project"
    subdir = project / "packages" / "app"
    subdir.mkdir(parents=True)
    (project / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
    monkeypatch.setattr(prompt_module, "_run_git", lambda _cwd, *args: None)

    context = build_project_context(cwd=subdir)

    assert f"project root: {project.resolve(strict=False)}" in context
    assert "project files:\n  package.json" in context


def test_project_context_includes_bounded_git_status(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    responses: dict[tuple[str, ...], str] = {
        ("rev-parse", "--is-inside-work-tree"): "true",
        ("branch", "--show-current"): "feature/test",
        ("status", "--short"): "\n".join(f" M file-{index}.py" for index in range(20)),
    }

    def fake_run_git(_cwd: Path, *args: str) -> str | None:
        return responses.get(args)

    monkeypatch.setattr(prompt_module, "_run_git", fake_run_git)

    context = build_project_context(cwd=tmp_path)

    assert "git: branch feature/test; 20 changed file(s)" in context
    assert "M file-0.py" in context
    assert "... 8 more" in context
    assert "M file-19.py" not in context


def test_project_context_is_bounded(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    def fake_run_git(_cwd: Path, *args: str) -> str | None:
        if args == ("rev-parse", "--is-inside-work-tree"):
            return "true"
        if args == ("branch", "--show-current"):
            return "feature/very-long-context"
        if args == ("status", "--short"):
            return "\n".join(f" M very-long-file-name-{index}.py" for index in range(50))
        return None

    monkeypatch.setattr(prompt_module, "_run_git", fake_run_git)

    context = build_project_context(cwd=tmp_path, max_chars=120)

    assert len(context) <= 120
    assert context.endswith("[context truncated]")


def test_project_context_honors_tiny_bounds(tmp_path: Path) -> None:
    context = build_project_context(cwd=tmp_path, max_chars=8)

    assert context == "[context"
