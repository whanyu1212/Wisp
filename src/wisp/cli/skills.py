"""CLI inspection for trust-aware Agent Skills metadata."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from wisp.agent.prompt import resolve_project_context_root
from wisp.cli.trust import resolve_cli_trust
from wisp.config import WispConfig
from wisp.skills import SkillCatalog, SkillDiagnostic, discover_skills
from wisp.skills.package import bundled_skills_root


def skills_command(
    project: Annotated[
        Path | None,
        typer.Argument(help="Project path to inspect. Defaults to the current project."),
    ] = None,
) -> None:
    """List valid Agent Skills and isolated discovery diagnostics."""

    selected = project or Path(".")
    resolution_diagnostics: tuple[SkillDiagnostic, ...] = ()
    try:
        selected = selected.expanduser()
        project_root = resolve_project_context_root(selected)
    except (OSError, RuntimeError) as exc:
        project_root = None
        message = str(exc) or type(exc).__name__
        resolution_diagnostics = (
            SkillDiagnostic(
                code="root-unreadable",
                severity="error",
                message=f"cannot resolve project root: {message}",
                source="project:wisp",
                path=selected / ".wisp" / "skills",
            ),
            SkillDiagnostic(
                code="root-unreadable",
                severity="error",
                message=f"cannot resolve project root: {message}",
                source="project:agents",
                path=selected / ".agents" / "skills",
            ),
        )

    trust = resolve_cli_trust(project_root) if project_root is not None else None
    trusted = trust.trusted if trust is not None else False
    config = WispConfig.from_env(project_dir=project_root, trusted=trusted)
    catalog = discover_skills(
        home_dir=_home_dir(),
        project_root=project_root if trusted else None,
        protected_paths=config.protected_paths,
        package_root=bundled_skills_root(),
    )
    if resolution_diagnostics:
        catalog = SkillCatalog(
            entries=catalog.entries,
            diagnostics=(*resolution_diagnostics, *catalog.diagnostics),
        )
    _render_catalog(catalog)
    if project_root is None:
        typer.echo(
            f"Project skills skipped because {_display_text(selected)} could not be resolved."
        )
    elif not trusted:
        typer.echo(f"Project skills skipped because {_display_text(project_root)} is not trusted.")


def _home_dir() -> Path:
    return Path.home()


def _render_catalog(catalog: SkillCatalog) -> None:
    if catalog.entries:
        typer.echo(f"Skills ({len(catalog.entries)}):")
        for entry in catalog.entries:
            description = _display_text(entry.description)
            typer.echo(f"{entry.name} [{entry.source}]")
            typer.echo(f"  {description}")
    else:
        typer.echo("No skills found.")

    if catalog.diagnostics:
        typer.echo("Diagnostics:")
        for diagnostic in catalog.diagnostics:
            location = f" ({_display_text(diagnostic.path)})" if diagnostic.path is not None else ""
            typer.echo(
                f"- {diagnostic.severity} {diagnostic.code} [{diagnostic.source}]"
                f"{location}: {_display_text(diagnostic.message)}"
            )


def _display_text(value: object) -> str:
    printable = "".join(character if character.isprintable() else " " for character in str(value))
    return " ".join(printable.split())
