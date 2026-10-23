"""Select CI checks conservatively and enforce the required status without dependencies."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

GROUP_JOBS = {
    "python": ("quality", "python-tests", "macos-smoke"),
    "rust": ("rust-workspace",),
    "handoff": ("tui-handoff",),
    "package": ("package",),
    "contracts": ("contracts",),
}
FULL = frozenset({"python", "rust", "handoff", "package"})
PYTHON = frozenset({"python", "package"})


def select_groups(paths: list[str]) -> set[str]:
    """Select checks for changed paths; unknown or empty input keeps full coverage.

    Args:
        paths (list[str]): Both old and new paths for renames, including deletions.

    Returns:
        set[str]: Groups to run. Full Python tests already include the contracts.
    """
    selected: set[str] = set()
    for path in paths:
        # These inputs cross language/test boundaries. Do not infer a narrower
        # scope from their extension or their parent directory.
        if path.startswith(
            ("schemas/", "tests/fixtures/", "tests/support/", "src/wisp/events/", "src/wisp/rpc/")
        ):
            selected.update(FULL)
        elif path.startswith("rust/wisp-tui/src/") and path.endswith(".rs"):
            selected.update({"rust", "handoff", "contracts"})
        elif path.startswith("src/wisp/"):
            selected.update(PYTHON | {"handoff"})
        elif path.startswith(("tests/tui_e2e/", "tests/benchmarks/")):
            selected.update(PYTHON | {"handoff"})
        elif path.startswith(("tests/", "examples/")) and path != "tests/conftest.py":
            selected.update(PYTHON)
        elif path == "README.md":
            # The README is also distribution metadata.
            selected.update({"contracts", "package"})
        elif path.startswith(("site/", "assets/")) or path in {
            "AGENTS.md",
            "CHANGELOG.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
        }:
            selected.add("contracts")
        else:
            # Includes CI definitions, scripts, manifests, lockfiles, toolchains,
            # native extension crates, and any new/unclassified repository area.
            selected.update(FULL)
    if not paths:
        selected.update(FULL)
    if "python" in selected:
        selected.discard("contracts")
    return selected


def changed_paths(event_name: str, event: dict[str, Any]) -> list[str]:
    """Read the complete Git diff, retaining both sides of renames.

    Args:
        event_name (str): GitHub event name.
        event (dict[str, Any]): GitHub event payload.

    Returns:
        list[str]: Changed paths, or an empty list to select all checks when no
        reliable diff is available or the workflow was dispatched manually.
    """
    if event_name == "pull_request":
        base = event["pull_request"]["base"]["sha"]
        head = event["pull_request"]["head"]["sha"]
        separator = "..."
    elif event_name == "push":
        base, head = event["before"], event["after"]
        separator = ".."
    else:
        return []
    if not all(isinstance(ref, str) and re.fullmatch(r"[0-9a-f]{40}", ref) for ref in (base, head)):
        raise ValueError("Expected commit SHAs in the GitHub event")
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--no-renames", "-z", f"{base}{separator}{head}", "--"],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        # New/force-pushed branches may not have a fetchable before commit.
        print("No reliable diff available; running all checks", file=sys.stderr)
        return []
    return [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]


def verify_results(needs: dict[str, Any]) -> list[str]:
    """Reject failed selection, missing jobs, and every unexpected job result.

    Args:
        needs (dict[str, Any]): GitHub's complete needs context for the final job.

    Returns:
        list[str]: Errors preventing the required check from passing.
    """
    if needs.get("changes", {}).get("result") != "success":
        return ["Check selection did not succeed"]
    outputs = needs["changes"].get("outputs", {})
    expected_jobs = {"changes"} | {job for jobs in GROUP_JOBS.values() for job in jobs}
    errors = []
    if set(needs) != expected_jobs:
        errors.append("Required-check dependencies do not match the known jobs")
    for group, jobs in GROUP_JOBS.items():
        if outputs.get(group) not in {"true", "false"}:
            errors.append(f"Missing or invalid selection for {group}")
            continue
        expected = "success" if outputs[group] == "true" else "skipped"
        for job in jobs:
            actual = needs.get(job, {}).get("result")
            if actual != expected:
                errors.append(f"{job}: expected {expected}, got {actual}")
    return errors


def main() -> int:
    """Run the selector or required-check verifier using GitHub's environment."""
    if sys.argv[1:] == ["verify"]:
        errors = verify_results(json.loads(os.environ["CI_NEEDS"]))
        for error in errors:
            print(error, file=sys.stderr)
        return int(bool(errors))
    if sys.argv[1:] != ["select"]:
        raise SystemExit("usage: ci_jobs.py select|verify")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    selected = select_groups(changed_paths(os.environ["GITHUB_EVENT_NAME"], event))
    output = "".join(f"{group}={str(group in selected).lower()}\n" for group in GROUP_JOBS)
    print(output, end="")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as destination:
        destination.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
