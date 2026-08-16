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
from enum import StrEnum

from rich.cells import cell_len

from wisp.events import ToolApprovalRequested, TrustRequested

_DECISION_PREVIEW_LINES = 5
_DECISION_PREVIEW_CHARS = 320
_DECISION_NOTICE_LINES = 9
_DECISION_NOTICE_CHARS = 640
_DECISION_TITLE_CELLS = 96
_DECISION_META_LINE_CELLS = 160
_BASH_OPERATION_TITLES = {
    "run": "Run command?",
    "start": "Start command?",
    "poll": "Poll process?",
    "cancel": "Cancel process?",
}


class _DecisionRole(StrEnum):
    READ = "read"
    MUTATING = "mutating"
    COMMAND = "command"
    TRUST = "trust"
    FALLBACK = "fallback"


@dataclass(frozen=True)
class _DecisionTreatment:
    role: _DecisionRole
    symbol: str
    label: str
    console_style: str


_READ_TREATMENT = _DecisionTreatment(
    role=_DecisionRole.READ,
    symbol="○",
    label="READ-ONLY ACCESS",
    console_style="cyan",
)
_MUTATING_TREATMENT = _DecisionTreatment(
    role=_DecisionRole.MUTATING,
    symbol="△",
    label="MUTATING OPERATION",
    console_style="yellow",
)
_COMMAND_TREATMENT = _DecisionTreatment(
    role=_DecisionRole.COMMAND,
    symbol="!",
    label="COMMAND EXECUTION",
    console_style="bold red",
)
_TRUST_TREATMENT = _DecisionTreatment(
    role=_DecisionRole.TRUST,
    symbol="◆",
    label="PROJECT TRUST",
    console_style="magenta",
)
_FALLBACK_TREATMENT = _DecisionTreatment(
    role=_DecisionRole.FALLBACK,
    symbol="?",
    label="TOOL APPROVAL",
    console_style="yellow",
)
_SAFETY_TREATMENTS = {
    "read": _READ_TREATMENT,
    "mutating": _MUTATING_TREATMENT,
    "command": _COMMAND_TREATMENT,
}


@dataclass(frozen=True)
class _DecisionContent:
    role: _DecisionRole
    title: str
    meta: str
    detail: str
    console_style: str


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


# The DecisionPanel's #decision-options viewport is a fixed 4 lines (one per
# option) with no auto-scroll to the highlighted option once the default
# highlight is no longer the last one. A long tool name in the "Allow <name>
# for this session" option can wrap to a second line, pushing "4 Deny" out of
# the visible viewport with nothing to scroll it back into view. Bound display
# cells, not code points, so wide Unicode extension names stay within the same
# narrow-terminal budget as plain ASCII names.
_TOOL_SESSION_OPTION_NAME_CELLS = 30


def _bounded_single_line(value: str, *, max_cells: int) -> str:
    """Collapse controls and truncate untrusted text to a terminal-cell budget."""

    normalized = value.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    candidate = normalized[:max_cells]
    truncated = len(normalized) > len(candidate) or cell_len(candidate) > max_cells
    if not truncated:
        return candidate

    ellipsis = "…"
    content_cells = max(0, max_cells - cell_len(ellipsis))
    while candidate and cell_len(candidate) > content_cells:
        candidate = candidate[:-1]
    return f"{candidate}{ellipsis}"


def _bounded_tool_session_option_name(name: str) -> str:
    """Truncate a tool name for the fixed-height tool-session option."""

    return _bounded_single_line(name, max_cells=_TOOL_SESSION_OPTION_NAME_CELLS)


def _treatment_for_safety(safety: str) -> _DecisionTreatment:
    """Map authoritative event metadata to presentation without policy inference."""

    return _SAFETY_TREATMENTS.get(safety, _FALLBACK_TREATMENT)


def _decision_meta(
    treatment: _DecisionTreatment,
    *,
    subject: str,
    cwd: str | None,
    label: str | None = None,
) -> str:
    category = label or treatment.label
    first_line = f"{treatment.symbol} {category}"
    if subject:
        first_line = f"{first_line} · {subject}"
    first_line = _bounded_single_line(first_line, max_cells=_DECISION_META_LINE_CELLS)
    if cwd:
        cwd_line = _bounded_single_line(f"cwd: {cwd}", max_cells=_DECISION_META_LINE_CELLS)
        return f"{first_line}\n{cwd_line}"
    return first_line


def _decision_notice(content: _DecisionContent) -> str:
    """Return one bounded, literal notice shared by non-Textual renderers."""

    lines: list[str] = []
    for part in (content.meta, content.title, content.detail):
        if part:
            lines.extend(part.splitlines())
    return _bounded_decision_preview(
        lines,
        max_lines=_DECISION_NOTICE_LINES,
        max_chars=_DECISION_NOTICE_CHARS,
    )


def _approval_content(
    event: ToolApprovalRequested,
    *,
    cwd: str | None = None,
) -> _DecisionContent:
    arguments = event.arguments
    treatment = _treatment_for_safety(event.safety)
    tool_name = _bounded_single_line(event.name, max_cells=_DECISION_TITLE_CELLS)

    if event.name == "bash":
        operation_value = arguments.get("operation")
        operation = operation_value if isinstance(operation_value, str) else "run"
        title = _BASH_OPERATION_TITLES.get(operation, "Run bash operation?")
        if operation in {"poll", "cancel"}:
            lines = [f"operation: {operation}"]
            process_id = arguments.get("process_id")
            process_id_text = process_id if isinstance(process_id, str) else "<missing>"
            lines.append(f"process_id: {process_id_text}")
            wait_seconds = arguments.get("wait_seconds")
            if operation == "poll" and wait_seconds is not None:
                lines.append(f"wait_seconds: {wait_seconds}s")
        else:
            command = arguments.get("command")
            command_text = command if isinstance(command, str) else ""
            lines = [
                f"$ {line}" if index == 0 else f"  {line}"
                for index, line in enumerate(command_text.splitlines() or [""])
            ]
            timeout = arguments.get("timeout")
            if timeout is not None:
                lines.append(f"timeout: {timeout}s")
            if operation == "start":
                lifetime_seconds = arguments.get("lifetime_seconds")
                if lifetime_seconds is not None:
                    lines.append(f"lifetime_seconds: {lifetime_seconds}s")
                yield_seconds = arguments.get("yield_seconds")
                if yield_seconds is not None:
                    lines.append(f"yield_seconds: {yield_seconds}s")
        return _DecisionContent(
            role=treatment.role,
            title=title,
            meta=_decision_meta(treatment, subject="bash", cwd=cwd),
            detail=_bounded_decision_preview(lines),
            console_style=treatment.console_style,
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
        category = "MODIFIES FILES" if treatment.role is _DecisionRole.MUTATING else None
        return _DecisionContent(
            role=treatment.role,
            title="Write file?",
            meta=_decision_meta(
                treatment,
                subject=path_text,
                cwd=cwd,
                label=category,
            ),
            detail=_bounded_decision_preview(lines),
            console_style=treatment.console_style,
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
        category = "MODIFIES FILES" if treatment.role is _DecisionRole.MUTATING else None
        return _DecisionContent(
            role=treatment.role,
            title="Edit file?",
            meta=_decision_meta(
                treatment,
                subject=path_text,
                cwd=cwd,
                label=category,
            ),
            detail=_bounded_decision_preview(lines),
            console_style=treatment.console_style,
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
        role=treatment.role,
        title=_bounded_single_line(
            f"Allow {tool_name}?",
            max_cells=_DECISION_TITLE_CELLS,
        ),
        meta=_decision_meta(treatment, subject=tool_name, cwd=cwd),
        detail=_bounded_decision_preview(serialized.splitlines()),
        console_style=treatment.console_style,
    )


def _trust_content(event: TrustRequested) -> _DecisionContent:
    return _DecisionContent(
        role=_TRUST_TREATMENT.role,
        title="Trust this project?",
        meta=_decision_meta(
            _TRUST_TREATMENT,
            subject=str(event.project_path),
            cwd=None,
        ),
        detail=(
            "Trusting loads project-controlled settings, instructions, and skills. "
            "It does not bypass tool approvals."
        ),
        console_style=_TRUST_TREATMENT.console_style,
    )
