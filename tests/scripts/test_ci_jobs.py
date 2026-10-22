"""Run with stdlib unittest in the selector job, before installing dependencies."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.ci_jobs import FULL, GROUP_JOBS, changed_paths, select_groups, verify_results


class CheckSelectionTests(unittest.TestCase):
    def test_docs_and_site_skip_runtime_but_keep_contracts(self) -> None:
        for path in ("book/src/guide/tui.md", "site/src/styles/site.css", "assets/banner.png"):
            with self.subTest(path=path):
                self.assertEqual(select_groups([path]), {"contracts"})
        self.assertEqual(select_groups(["README.md"]), {"contracts", "package"})

    def test_tui_source_keeps_rust_handoff_and_repository_contracts(self) -> None:
        self.assertEqual(
            select_groups(["rust/wisp-tui/src/ui.rs", "book/src/guide/tui.md"]),
            {"rust", "handoff", "contracts"},
        )

    def test_python_runtime_keeps_both_platforms_and_handoff(self) -> None:
        self.assertEqual(
            select_groups(["src/wisp/agent/loop/runner.py"]), {"python", "package", "handoff"}
        )

    def test_shared_and_unknown_inputs_keep_all_checks(self) -> None:
        for path in (
            "pyproject.toml",
            "uv.lock",
            ".python-version",
            "Cargo.lock",
            "Cargo.toml",
            "rust-toolchain.toml",
            ".cargo/config.toml",
            "tests/conftest.py",
            "tests/support/tui.py",
            "tests/fixtures/rust_tui_context.json",
            "schemas/live-rpc/events.schema.json",
            "src/wisp/events/run.py",
            "src/wisp/rpc/commands.py",
            "rust/wisp-python/src/lib.rs",
            "scripts/ci_jobs.py",
            ".github/workflows/ci.yml",
            "new-area/file.txt",
        ):
            with self.subTest(path=path):
                self.assertEqual(select_groups([path]), FULL)
        self.assertEqual(select_groups([]), FULL)

    def test_mixed_change_unions_checks_without_duplicate_contracts(self) -> None:
        self.assertEqual(
            select_groups(["rust/wisp-tui/src/ui.rs", "src/wisp/config/runtime.py"]), FULL
        )

    def test_handoff_and_benchmark_test_changes_run_with_binary(self) -> None:
        for path in ("tests/tui_e2e/test_input.py", "tests/benchmarks/test_tui_hydration.py"):
            self.assertIn("handoff", select_groups([path]))

    def test_git_diff_keeps_deleted_and_renamed_paths_and_uses_merge_base(self) -> None:
        # No GitHub API pagination/300-file limit; filenames are NUL-separated.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args: str) -> str:
                return subprocess.check_output(["git", "-C", directory, *args], text=True).strip()

            git("init", "-q")
            git("config", "user.name", "CI tests")
            git("config", "user.email", "ci@example.invalid")
            (root / "src/wisp").mkdir(parents=True)
            (root / "src/wisp/removed.py").write_text("old")
            git("add", ".")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            (root / "book").mkdir()
            (root / "src/wisp/removed.py").rename(root / "book/renamed.md")
            for index in range(350):
                (root / f"book/{index}.md").write_text("docs")
            (root / "book/line\nbreak.md").write_text("docs")
            git("add", ".")
            git("commit", "-qm", "head")
            head = git("rev-parse", "HEAD")
            git("checkout", "-q", base)
            (root / "base-only.txt").write_text("not part of PR")
            git("add", ".")
            git("commit", "-qm", "base advanced")
            advanced = git("rev-parse", "HEAD")
            with patch("scripts.ci_jobs.subprocess.run", wraps=subprocess.run) as run:
                # Supply cwd without changing the test process's working directory.
                run.side_effect = lambda *a, **kw: subprocess_run(*a, cwd=directory, **kw)
                paths = changed_paths(
                    "pull_request",
                    {"pull_request": {"base": {"sha": advanced}, "head": {"sha": head}}},
                )
                self.assertEqual(len(paths), 353)
                self.assertIn("src/wisp/removed.py", paths)
                self.assertIn("book/line\nbreak.md", paths)
                self.assertNotIn("base-only.txt", paths)
                self.assertIn("python", select_groups(paths))
                self.assertEqual(
                    set(changed_paths("push", {"before": base, "after": head})), set(paths)
                )

    def test_manual_and_unavailable_base_run_all_checks(self) -> None:
        self.assertEqual(select_groups(changed_paths("workflow_dispatch", {})), FULL)
        with patch(
            "scripts.ci_jobs.subprocess.run", side_effect=subprocess.CalledProcessError(128, "git")
        ):
            self.assertEqual(
                select_groups(changed_paths("push", {"before": "0" * 40, "after": "a" * 40})), FULL
            )


# Save before mocking subprocess.run (the module object is shared).
subprocess_run = subprocess.run


class RequiredCheckTests(unittest.TestCase):
    def needs(self, selected: set[str]) -> dict:
        outputs = {group: str(group in selected).lower() for group in GROUP_JOBS}
        needs = {"changes": {"result": "success", "outputs": outputs}}
        for group, jobs in GROUP_JOBS.items():
            for job in jobs:
                needs[job] = {"result": "success" if group in selected else "skipped"}
        return needs

    def test_intentionally_skipped_jobs_pass(self) -> None:
        self.assertEqual(verify_results(self.needs({"contracts"})), [])

    def test_full_suite_passes(self) -> None:
        self.assertEqual(verify_results(self.needs(set(FULL))), [])

    def test_selected_job_failure_cancel_or_skip_blocks(self) -> None:
        for result in ("failure", "cancelled", "skipped", None):
            with self.subTest(result=result):
                needs = self.needs(set(FULL))
                needs["rust-workspace"]["result"] = result
                self.assertTrue(verify_results(needs))

    def test_failed_plan_or_missing_output_blocks(self) -> None:
        for result in ("failure", "cancelled", "skipped"):
            needs = self.needs(set())
            needs["changes"]["result"] = result
            self.assertTrue(verify_results(needs))
        needs = self.needs(set(FULL))
        del needs["changes"]["outputs"]["rust"]
        self.assertTrue(verify_results(needs))

    def test_missing_and_unaccounted_jobs_block(self) -> None:
        needs = self.needs(set(FULL))
        del needs["rust-workspace"]
        self.assertTrue(verify_results(needs))
        needs = self.needs(set(FULL))
        needs["new-job"] = {"result": "failure"}
        self.assertTrue(verify_results(needs))


if __name__ == "__main__":
    unittest.main()
