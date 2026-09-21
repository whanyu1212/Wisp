"""Historical bundle protection, also executable from a trusted CI checkout.

This guard checks artifact history, not whether a schema edit is semantically
additive. Schema generation, conformance tests, and compatibility review remain
separate requirements. Keep this module standard-library-only: the trusted
pull_request_target workflow executes it directly without installing PR code.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ArtifactChange:
    """One changed path and, for renames, its previous identity."""

    status: str
    path: str
    previous_path: str | None = None


def immutable_artifact_changes(
    changes: Sequence[ArtifactChange],
    base_paths: Sequence[str],
    *,
    root: str = "schemas/live-rpc",
) -> tuple[str, ...]:
    """Find forbidden edits relative to a trusted base artifact inventory.

    Args:
        changes (Sequence[ArtifactChange]): Complete changed-path inventory.
        base_paths (Sequence[str]): Files present in the trusted base.
        root (str): Repository-relative schema root.

    Returns:
        tuple[str, ...]: Sorted violating paths. Current-bundle additions and
        modifications are allowed only when no newer bundle is introduced.
        Removal, rename, and type changes are never allowed in existing bundles.
    """
    # GitHub can report a new destination as copied. Its source is unchanged;
    # local `git diff --no-renames` represents the same operation as an addition.
    changes = tuple(
        ArtifactChange("added", change.path) if change.status == "copied" else change
        for change in changes
    )
    prefix = root.rstrip("/") + "/"

    def version(path: str) -> int | None:
        directory = path.removeprefix(prefix).partition("/")[0]
        if not directory.startswith("v") or not directory[1:].isascii():
            return None
        digits = directory[1:]
        if not digits.isdigit() or digits.startswith("0") or len(digits) > 9:
            return None
        return int(digits)

    base_versions = {version(path) for path in base_paths if path.startswith(prefix)}
    if None in base_versions or not base_versions:
        return ("cannot verify immutable protocol history: invalid trusted inventory",)
    current = max(value for value in base_versions if value is not None)
    changed_paths = [
        path
        for change in changes
        for path in (change.path, change.previous_path)
        if path is not None and path.startswith(prefix)
    ]
    introduces_newer = any((version(path) or 0) > current for path in changed_paths)
    violations: set[str] = set()
    for change in changes:
        for path in (change.path, change.previous_path):
            if path is None or not path.startswith(prefix):
                continue
            number = version(path)
            if number is not None:
                if number > current and change.status == "added" and change.previous_path is None:
                    continue
                if (
                    number == current
                    and not introduces_newer
                    and change.status in {"added", "modified"}
                    and change.previous_path is None
                ):
                    continue
            violations.add(path)
    return tuple(sorted(violations))


def main() -> int:
    """Check paginated GitHub file metadata against the trusted checkout.

    Returns:
        int: Zero for allowed changes, one for violations or invalid input.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changes-json", type=Path, required=True)
    parser.add_argument("--expected-files", type=int, required=True)
    args = parser.parse_args()
    try:
        if not 0 <= args.expected_files <= 3000:
            raise ValueError("cannot verify more than 3,000 changed files")
        pages = json.loads(args.changes_json.read_text(encoding="utf-8"))
        if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
            raise ValueError("expected paginated GitHub file lists")
        changes = []
        for page in pages:
            for item in page:
                if not isinstance(item, dict):
                    raise ValueError("invalid GitHub file metadata")
                status, path, previous = (
                    item.get("status"),
                    item.get("filename"),
                    item.get("previous_filename"),
                )
                if not isinstance(status, str) or not isinstance(path, str):
                    raise ValueError("invalid GitHub file metadata")
                if previous is not None and not isinstance(previous, str):
                    raise ValueError("invalid previous filename")
                changes.append(ArtifactChange(status, path, previous))
        if len(changes) != args.expected_files:
            raise ValueError("changed-file count differs from the event; retry the check")
        paths = [
            path.as_posix() for path in Path("schemas/live-rpc").glob("v*/*") if path.is_file()
        ]
        violations = immutable_artifact_changes(changes, paths)
    except (OSError, ValueError) as exc:
        print(f"cannot verify immutable protocol history: {exc}")
        return 1
    for path in violations:
        print(f"committed protocol artifact is immutable: {path}")
    return int(bool(violations))


if __name__ == "__main__":
    raise SystemExit(main())
