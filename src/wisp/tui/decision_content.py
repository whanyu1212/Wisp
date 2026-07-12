"""Content builders for the approval/trust decision surface.

Pure functions that translate a `ToolApprovalRequested` / `TrustRequested` event
into the title/meta/detail text the `DecisionPanel` renders. This is approval-
domain *presentation* logic — bounded previews, per-tool argument formatting,
safety labels — kept out of the widget module so the panel stays about layout and
these stay about what to show. No widget or app state is touched.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from wisp.events import ToolApprovalRequested, TrustRequested
from wisp.tui.rendering import _format_cwd_for_footer

_DECISION_PREVIEW_LINES = 5
_DECISION_PREVIEW_CHARS = 320


@dataclass(frozen=True)
class _DecisionContent:
    title: str
    meta: str
    detail: str


def _bounded_decision_preview(
    lines: list[str],
    *,
    max_lines: int = _DECISION_PREVIEW_LINES,
    max_chars: int = _DECISION_PREVIEW_CHARS,
) -> str:
    """Return a compact preview with an explicit final truncation marker."""

    normalized: list[str] = []
    for line in lines:
        normalized.extend(line.replace("\r\n", "\n").replace("\r", "\n").split("\n"))

    selected: list[str] = []
    used_chars = 0
    truncated = False
    for line in normalized:
        if len(selected) >= max_lines or used_chars >= max_chars:
            truncated = True
            break
        remaining = max_chars - used_chars
        if len(line) > remaining:
            selected.append(line[:remaining])
            used_chars += remaining
            truncated = True
            break
        selected.append(line)
        used_chars += len(line)

    if len(selected) < len(normalized):
        truncated = True
    if not selected:
        selected.append("")
    if truncated:
        if len(selected) >= max_lines:
            selected[-1] = "... preview truncated"
        else:
            selected.append("... preview truncated")
    return "\n".join(selected)


def _safety_label(safety: str) -> str:
    return {
        "read": "read-only access",
        "mutating": "file mutation",
        "command": "command execution",
    }.get(safety, safety)


def _approval_content(event: ToolApprovalRequested, *, cwd: str) -> _DecisionContent:
    arguments = event.arguments
    cwd_text = _format_cwd_for_footer(cwd)
    safety = _safety_label(event.safety)

    if event.name == "bash":
        command = arguments.get("command")
        command_text = command if isinstance(command, str) else ""
        lines = [
            f"$ {line}" if index == 0 else f"  {line}"
            for index, line in enumerate(command_text.splitlines() or [""])
        ]
        timeout = arguments.get("timeout")
        if timeout is not None:
            lines.append(f"timeout: {timeout}s")
        return _DecisionContent(
            title="Run command?",
            meta=f"bash - {safety}\ncwd: {cwd_text}",
            detail=_bounded_decision_preview(lines),
        )

    if event.name == "write":
        path = arguments.get("path")
        path_text = path if isinstance(path, str) else "unknown path"
        content = arguments.get("content")
        content_text = content if isinstance(content, str) else ""
        line_count = len(content_text.splitlines())
        byte_count = len(content_text.encode("utf-8"))
        lines = [f"content: {line_count} lines, {byte_count} bytes"]
        lines.extend(content_text.splitlines())
        return _DecisionContent(
            title="Write file?",
            meta=f"{path_text}\n{safety} - cwd: {cwd_text}",
            detail=_bounded_decision_preview(lines),
        )

    if event.name == "edit":
        path = arguments.get("path")
        path_text = path if isinstance(path, str) else "unknown path"
        edits = arguments.get("edits")
        edit_items = edits if isinstance(edits, list) else []
        lines = [f"replacements: {len(edit_items)}"]
        for item in edit_items[:2]:
            if not isinstance(item, Mapping):
                continue
            old_text = item.get("oldText")
            new_text = item.get("newText")
            old_line = old_text if isinstance(old_text, str) else ""
            new_line = new_text if isinstance(new_text, str) else ""
            lines.append(f"- {old_line}")
            lines.append(f"+ {new_line}")
        if len(edit_items) > 2:
            lines.append(f"... {len(edit_items) - 2} more replacements")
        return _DecisionContent(
            title="Edit file?",
            meta=f"{path_text}\n{safety} - cwd: {cwd_text}",
            detail=_bounded_decision_preview(lines),
        )

    try:
        serialized = json.dumps(
            arguments,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
    except (TypeError, ValueError):
        serialized = json.dumps(str(arguments), ensure_ascii=False)
    return _DecisionContent(
        title=f"Allow {event.name}?",
        meta=f"{safety} - cwd: {cwd_text}",
        detail=_bounded_decision_preview(serialized.splitlines()),
    )


def _trust_content(event: TrustRequested) -> _DecisionContent:
    return _DecisionContent(
        title="Trust this project?",
        meta=str(event.project_path),
        detail="Trusting allows project-local settings and instructions to load.",
    )
