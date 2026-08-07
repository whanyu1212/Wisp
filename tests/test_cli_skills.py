"""Tests for the user-facing Agent Skills catalog command."""

from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch
from typer.testing import CliRunner

from wisp.cli import app
from wisp.cli import skills as skills_module


def _write_skill(root: Path, name: str, description: str) -> None:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# Instructions\n",
        encoding="utf-8",
    )


def _env(home: Path, *, trusted: str | None = None) -> dict[str, str]:
    env = {
        "HOME": str(home),
        "WISP_TRUST_FILE": str(home / "trust.json"),
    }
    if trusted is not None:
        env["WISP_TRUST"] = trusted
    return env


def test_skills_lists_user_catalog_in_name_order(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    _write_skill(home / ".wisp" / "skills", "zebra", "Zebra tasks.")
    _write_skill(home / ".agents" / "skills", "alpha", "Alpha\n  tasks.")
    monkeypatch.setattr(skills_module, "_home_dir", lambda: home)

    result = CliRunner().invoke(app, ["skills"], env=_env(home, trusted="0"))

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith(
        "Skills (2):\nalpha [user:agents]\n  Alpha tasks.\nzebra [user:wisp]\n  Zebra tasks.\n"
    )
    assert "Project skills skipped" in result.stdout


def test_skills_includes_project_catalog_only_when_trusted(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    _write_skill(project / ".wisp" / "skills", "project-skill", "Project tasks.")
    monkeypatch.chdir(project)
    runner = CliRunner()

    untrusted = runner.invoke(app, ["skills"], env=_env(home, trusted="0"))
    trusted = runner.invoke(app, ["skills"], env=_env(home, trusted="1"))

    assert untrusted.exit_code == 0, untrusted.output
    assert "project-skill" not in untrusted.stdout
    assert "Project skills skipped" in untrusted.stdout
    assert trusted.exit_code == 0, trusted.output
    assert "project-skill [project:wisp]" in trusted.stdout
    assert "Project skills skipped" not in trusted.stdout


def test_skills_resolves_explicit_subdirectory_to_project_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    subdirectory = project / "packages" / "app"
    subdirectory.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    _write_skill(project / ".agents" / "skills", "project-skill", "Project tasks.")

    result = CliRunner().invoke(
        app,
        ["skills", str(subdirectory)],
        env=_env(home, trusted="1"),
    )

    assert result.exit_code == 0, result.output
    assert "project-skill [project:agents]" in result.stdout


def test_skills_reports_diagnostics_without_hiding_valid_entries(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    root = home / ".wisp" / "skills"
    _write_skill(root, "valid", "Valid tasks.")
    invalid = root / "invalid"
    invalid.mkdir(parents=True)
    (invalid / "SKILL.md").write_text("not frontmatter", encoding="utf-8")
    monkeypatch.setattr(skills_module, "_home_dir", lambda: home)

    result = CliRunner().invoke(app, ["skills"], env=_env(home, trusted="0"))

    assert result.exit_code == 0, result.output
    assert "valid [user:wisp]" in result.stdout
    assert "Diagnostics:" in result.stdout
    assert "invalid-frontmatter [user:wisp]" in result.stdout


def test_skills_reports_empty_catalog(tmp_path: Path) -> None:
    home = tmp_path / "home"

    result = CliRunner().invoke(app, ["skills"], env=_env(home, trusted="1"))

    assert result.exit_code == 0, result.output
    assert result.stdout == "No skills found.\n"


def test_skills_strips_terminal_control_characters(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    skill = home / ".wisp" / "skills" / "safe"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        '---\nname: safe\ndescription: "safe\\x1b[31m text"\n---\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(skills_module, "_home_dir", lambda: home)

    result = CliRunner().invoke(app, ["skills"], env=_env(home, trusted="0"))

    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.stdout
    assert "safe [31m text" in result.stdout
