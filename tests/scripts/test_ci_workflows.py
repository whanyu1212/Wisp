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
    contracts = next(
        step
        for step in steps
        if step.get("name") == "Verify documentation contracts and SDK examples"
    )
    # HOME isolation must not lose the container checkout's narrowly scoped Git trust.
    assert contracts["env"] == {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "${{ github.workspace }}",
    }
    for checkpoint in range(1, 6):
        assert f"python -m examples.crafting_agents.checkpoint_0{checkpoint}" in commands
    smoke = jobs["smoke"]["steps"][-1]
    assert smoke["env"]["WISP_DOCS_REVISION"] == "${{ github.sha }}"
    assert smoke["run"] == "npm run test:deployed"


def test_wheel_caches_use_fixed_action() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/rust-tui-wheels.yml").read_text())
    expected_action = "Swatinem/rust-cache@d1527c8f67ef9af357290475d4af4b0fab8f12e0"
    for job_name in ("linux-x86-64", "macos"):
        steps = workflow["jobs"][job_name]["steps"]
        cache_step = next(step for step in steps if step.get("uses") == expected_action)
        assert cache_step["with"]["key"]

    macos_steps = workflow["jobs"]["macos"]["steps"]
    remove_idx = next(
        i for i, step in enumerate(macos_steps) if step.get("name") == "Remove Homebrew rustup"
    )
    install_idx = next(
        i
        for i, step in enumerate(macos_steps)
        if str(step.get("uses", "")).startswith("dtolnay/rust-toolchain@")
    )
    cache_idx = next(i for i, step in enumerate(macos_steps) if step.get("uses") == expected_action)
    assert remove_idx < install_idx < cache_idx


def test_fuzz_warms_main_cache() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/rust-fuzz.yml").read_text())
    triggers = workflow.get("on") or workflow[True]
    assert triggers["push"]["branches"] == ["main"]
    assert triggers["push"]["paths"] == triggers["pull_request"]["paths"]
    steps = workflow["jobs"]["fuzz"]["steps"]
    assert any(
        step.get("uses") == "Swatinem/rust-cache@d1527c8f67ef9af357290475d4af4b0fab8f12e0"
        for step in steps
    )


def test_python_matrices_cover_every_shard() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
    for job_name, expected_serial_shards in (("python-tests", 3), ("python-tests-oldest", 2)):
        job = workflow["jobs"][job_name]
        entries = {entry["suite"]: entry["args"] for entry in job["strategy"]["matrix"]["include"]}

        for idx in (1, 2):
            args = entries[f"parallel-{idx}"]
            assert "-m 'not (slow or process or benchmark)'" in args
            assert "-n auto" in args
            assert f"--shard {idx}/2" in args

        for idx in range(1, expected_serial_shards + 1):
            args = entries[f"serial-{idx}"]
            assert "-m 'slow or process or benchmark'" in args
            assert "-k 'not test_every_module_imports_fresh'" in args
            assert f"--shard {idx}/{expected_serial_shards}" in args

        fresh = entries["fresh-imports"]
        assert "tests/repository/test_architecture.py" in fresh
        assert "test_every_module_imports_fresh" in fresh

        test_step = next(step for step in job["steps"] if step.get("name") == "Test")
        assert "uv run pytest ${{ matrix.args }}" in test_step["run"]


def test_handoff_runs_tui_benchmarks() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["tui-handoff"]["steps"]
    e2e_step = next(step for step in steps if step.get("name") == "Test Python/Rust TUI handoff")
    bench_step = next(step for step in steps if step.get("name") == "Test TUI benchmark harnesses")
    assert "tests/tui_e2e" in e2e_step["run"]
    assert "tests/benchmarks/test_tui_*.py" in bench_step["run"]


def test_macos_smoke_covers_platform_paths() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["macos-smoke"]["steps"]
    parallel = next(
        step for step in steps if step.get("name") == "Test platform-sensitive paths (parallel)"
    )
    serial = next(
        step for step in steps if step.get("name") == "Test timing-sensitive macOS paths (serial)"
    )

    for path in (
        "tests/auth/test_storage.py",
        "tests/cli/test_rpc_transport.py",
        "tests/tools/files/test_secure_fs.py",
        "tests/tools/test_seatbelt.py",
        "tests/tools/test_shell_auto_workflows.py",
        "tests/tools/shell/test_working_directory.py",
        "tests/coding/test_sandbox_process_approvals.py",
        "tests/sessions",
    ):
        assert path in parallel["run"]
    assert "-m 'not benchmark'" in parallel["run"]
    assert "-n 2" in parallel["run"]

    serial_commands = serial["run"].replace("\\\n", " ").splitlines()
    supervisor_cmd = next(
        cmd for cmd in serial_commands if "tests/tools/shell/test_supervisor*.py" in cmd
    )
    benchmark_cmd = next(cmd for cmd in serial_commands if "tests/sessions" in cmd)
    assert "tests/mcp/test_runtime_process.py" in supervisor_cmd
    assert "-m benchmark" not in supervisor_cmd
    assert "tests/sessions -m benchmark" in benchmark_cmd


def test_rust_lint_splits_from_workspace() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
    jobs = workflow["jobs"]
    expected_cache = "Swatinem/rust-cache@d1527c8f67ef9af357290475d4af4b0fab8f12e0"
    expected_keys = {
        "rust-lint": "rust-lint-ubuntu-latest",
        "rust-workspace": "rust-test-${{ matrix.os }}",
        "tui-handoff": "tui-handoff-v2-${{ matrix.os }}",
    }
    for job_name, cache_key in expected_keys.items():
        cache_step = next(
            step
            for step in jobs[job_name]["steps"]
            if step.get("name") == "Cache Rust dependencies"
        )
        assert cache_step["uses"] == expected_cache
        assert cache_step["with"]["key"] == cache_key
        assert cache_step["with"]["cache-bin"] is False

    lint = jobs["rust-lint"]
    assert lint["runs-on"] == "ubuntu-latest"
    assert lint["env"]["PYO3_NO_PYTHON"] == "1"
    lint_steps = lint["steps"]
    assert not any(
        str(step.get("uses", "")).startswith(("astral-sh/setup-uv@", "actions/setup-python@"))
        or "uv sync" in step.get("run", "")
        for step in lint_steps
    )
    lint_toolchain = next(
        step
        for step in lint_steps
        if str(step.get("uses", "")).startswith("dtolnay/rust-toolchain@")
    )
    assert lint_toolchain["with"]["components"] == "clippy,rustfmt"
    lint_commands = [step.get("run", "") for step in lint_steps]
    assert "cargo fmt --all --check" in lint_commands
    assert "cargo clippy --workspace --all-targets --all-features -- -D warnings" in lint_commands

    workspace = jobs["rust-workspace"]
    assert workspace["strategy"]["matrix"]["os"] == ["ubuntu-latest", "macos-latest"]
    assert workspace["env"]["PYO3_NO_PYTHON"] == "1"
    ws_steps = workspace["steps"]
    assert not any(
        str(step.get("uses", "")).startswith(("astral-sh/setup-uv@", "actions/setup-python@"))
        or "uv sync" in step.get("run", "")
        for step in ws_steps
    )
    remove_idx = next(
        i for i, step in enumerate(ws_steps) if step.get("name") == "Remove Homebrew rustup"
    )
    install_idx = next(
        i
        for i, step in enumerate(ws_steps)
        if str(step.get("uses", "")).startswith("dtolnay/rust-toolchain@")
    )
    cache_idx = next(i for i, step in enumerate(ws_steps) if step.get("uses") == expected_cache)
    assert remove_idx < install_idx < cache_idx
    assert not any("cargo fmt" in step.get("run", "") for step in ws_steps)
    clippy_step = next(step for step in ws_steps if "cargo clippy" in step.get("run", ""))
    assert clippy_step["if"] == "runner.os == 'macOS'"
    assert (
        clippy_step["run"]
        == "cargo clippy -p wisp-search --all-targets --all-features -- -D warnings"
    )
    assert any(step.get("run") == "cargo test --workspace --all-features" for step in ws_steps)
