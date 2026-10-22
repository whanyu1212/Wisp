"""Check that workflow wiring enforces the selector's required-check policy."""

from __future__ import annotations

import json

import yaml

from scripts.ci_jobs import GROUP_JOBS
from tests.support.paths import REPO_ROOT


def test_required_status_accounts_for_every_workflow_job() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
    jobs = workflow["jobs"]
    gate = jobs["ci-passed"]
    assert gate["if"] == "${{ always() }}"
    assert set(gate["needs"]) == set(jobs) - {"ci-passed"}
    assert set(jobs) == {"changes", "ci-passed"} | {
        job for group in GROUP_JOBS.values() for job in group
    }
    assert set(jobs["changes"]["outputs"]) == set(GROUP_JOBS)
    for group, names in GROUP_JOBS.items():
        assert jobs["changes"]["outputs"][group] == f"${{{{ steps.select.outputs.{group} }}}}"
        for name in names:
            assert jobs[name]["needs"] == "changes"
            assert jobs[name]["if"] == f"needs.changes.outputs.{group} == 'true'"
    verify = gate["steps"][-1]
    assert verify["env"]["CI_NEEDS"] == "${{ toJSON(needs) }}"
    assert verify["run"] == "python3 scripts/ci_jobs.py verify"


def test_playwright_container_matches_locked_browser_version() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/astro.yml").read_text())
    lock = json.loads((REPO_ROOT / "site/package-lock.json").read_text())
    version = lock["packages"]["node_modules/@playwright/test"]["version"]
    job = workflow["jobs"]["check"]
    assert job["container"]["image"] == f"mcr.microsoft.com/playwright:v{version}-noble"
    check = next(
        step for step in job["steps"] if step.get("name") == "Verify Playwright image version"
    )
    assert f'!== "{version}"' in check["run"]
