"""Plain-text presentation helpers for Agent Skills."""

from __future__ import annotations

from wisp.events import RpcSkillCatalogSnapshot, SkillInvoked


def skill_invocation_text(event: SkillInvoked) -> str:
    """Return a compact, single-line label for a typed skill invocation."""

    return format_skill_invocation(
        event.invocation.name,
        event.invocation.request,
        instructions_truncated=event.invocation.instructions_truncated,
    )


def format_skill_invocation(
    name: str,
    request: str,
    *,
    request_truncated: bool = False,
    instructions_truncated: bool = False,
) -> str:
    """Return a compact invocation label from live or persisted typed fields."""

    request = " ".join(request.split())
    text = f"skill /skill:{name}"
    if request:
        text = f"{text} {request}"
    if request_truncated:
        text = f"{text} [request truncated]"
    if instructions_truncated:
        text = f"{text} [instructions truncated]"
    return text


def skill_catalog_text(catalog: RpcSkillCatalogSnapshot) -> str:
    """Render a catalog snapshot without terminal or Markdown markup."""

    project_status = "enabled" if catalog.project_trusted else "unavailable (project not trusted)"
    lines = [f"Agent Skills ({len(catalog.entries)})", f"Project skills: {project_status}"]
    if catalog.entries:
        for entry in catalog.entries:
            description = " ".join(entry.description.split())
            suffix = f" - {description}" if description else ""
            lines.append(f"/skill:{entry.name} [{entry.source}]{suffix}")
    else:
        lines.append("No skills discovered.")

    if catalog.diagnostics:
        lines.append("")
        lines.append(f"Diagnostics ({len(catalog.diagnostics)})")
        for diagnostic in catalog.diagnostics:
            location = f": {diagnostic.path}" if diagnostic.path is not None else ""
            lines.append(f"{diagnostic.source}{location}: {diagnostic.message}")

    return "\n".join(lines)
