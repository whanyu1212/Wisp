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
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/docs.yml").read_text())
    lock = json.loads((REPO_ROOT / "site/package-lock.json").read_text())
    version = lock["packages"]["node_modules/@playwright/test"]["version"]
    job = workflow["jobs"]["build"]
    assert workflow["jobs"]["smoke"]["container"]["image"] == (
        f"mcr.microsoft.com/playwright:v{version}-noble"
    )
    assert job["container"]["image"] == f"mcr.microsoft.com/playwright:v{version}-noble"
    check = next(
        step for step in job["steps"] if step.get("name") == "Verify Playwright image version"
    )
    assert f'!== "{version}"' in check["run"]


def test_docs_has_one_verified_serialized_deployment_path() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/docs.yml").read_text())
    assert not (REPO_ROOT / ".github/workflows/astro.yml").exists()
    assert not (REPO_ROOT / "book/book.toml").exists()
    jobs = workflow["jobs"]
    assert set(jobs) == {"build", "deploy", "smoke"}
    assert jobs["deploy"]["needs"] == "build"
    assert jobs["deploy"]["if"] == "github.event_name != 'pull_request'"
    assert jobs["smoke"]["needs"] == "deploy"
    assert (
        workflow["concurrency"]["cancel-in-progress"]
        == "${{ github.event_name == 'pull_request' }}"
    )
    assert "|| 'pages'" in workflow["concurrency"]["group"]
    steps = jobs["build"]["steps"]
    artifact = next(
        step for step in steps if step.get("uses") == "actions/upload-pages-artifact@v3"
    )
    assert artifact["with"]["path"] == "site/dist"
    assert artifact["if"] == "github.event_name != 'pull_request'"
    commands = "\n".join(step.get("run", "") for step in steps)
    assert "npm run build" in commands
    assert "npm run test:browser" in commands
    assert "pytest tests/docs tests/repository/test_images.py" in commands
    for checkpoint in range(1, 6):
        assert f"python -m examples.crafting_agents.checkpoint_0{checkpoint}" in commands
    smoke = jobs["smoke"]["steps"][-1]
    assert smoke["env"]["WISP_DOCS_REVISION"] == "${{ github.sha }}"
    assert smoke["run"] == "npm run test:deployed"
