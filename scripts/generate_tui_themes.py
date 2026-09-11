"""Generate the native palette catalog from Wisp's authoritative Python themes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from wisp.tui.commands import TEXTUAL_LOCAL_COMMAND_DESCRIPTORS
from wisp.tui.theme import DEFAULT_THEME_NAME, PAPER_THEME_NAME, WISP_THEME_SPECS

OUTPUT = Path(__file__).resolve().parents[1] / "rust/wisp-tui/src/theme_catalog.json"


def catalog_json() -> str:
    """Serialize ordered theme metadata and native semantic colors.

    Returns:
        str: Deterministic JSON derived from the existing Textual theme definitions.
    """
    themes = []
    for spec in WISP_THEME_SPECS:
        theme = spec.theme
        colors = {
            name: getattr(theme, name)
            for name in (
                "background",
                "foreground",
                "primary",
                "secondary",
                "accent",
                "success",
                "warning",
                "error",
                "surface",
                "panel",
            )
        }
        variables = theme.variables or {}
        colors.update(
            muted=variables["transcript-muted"],
            warning=variables.get("text-warning", theme.warning),
            error=variables.get("text-error", theme.error),
            addition=variables["diff-add-fg"],
            addition_background=variables["diff-add-bg"],
            deletion=variables["diff-del-fg"],
            deletion_background=variables["diff-del-bg"],
        )
        themes.append(
            {
                "name": spec.name,
                "slug": spec.slug,
                "label": spec.label,
                "description": spec.description,
                "dark": spec.dark,
                "colors": colors,
            }
        )
    command = next(item for item in TEXTUAL_LOCAL_COMMAND_DESCRIPTORS if item.name == "theme")
    return (
        json.dumps(
            {
                "_generated": "Run uv run python scripts/generate_tui_themes.py; do not edit.",
                "default": DEFAULT_THEME_NAME,
                "paper": PAPER_THEME_NAME,
                "command": {
                    "name": command.name,
                    "description": command.description,
                    "slash_command": command.slash_command,
                    "slash_aliases": command.slash_aliases,
                    "order": command.order,
                },
                "themes": themes,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def main() -> int:
    """Generate the catalog or report stale generated data without changing it.

    Returns:
        int: Zero on success, or one when check mode finds missing or stale data.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = catalog_json()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != expected:
            print("Rust theme catalog is stale; run uv run python scripts/generate_tui_themes.py")
            return 1
    else:
        OUTPUT.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
