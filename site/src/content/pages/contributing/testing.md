---
title: Testing
description: The complete suite runs against deterministic fake or scripted providers, so the agent core, CLI, and JSONL sessions are exercised without API keys, live model calls, or…
route: contributing/testing.html
section: contributing
order: 36
layout: docs
listed: true
searchable: true
---

# Testing

```bash
uv run pytest tests                                                  # complete suite
uv run pytest tests -m 'not (slow or process or benchmark)' -n auto  # quick subset
```

The complete suite runs against deterministic fake or scripted providers, so the agent core, CLI, and
JSONL sessions are exercised without API keys, live model calls, or provider credentials. Run the
complete command before considering a change verified.

## Layout

Unit and integration tests mirror the source tree: tests for `src/wisp/<package>/<module>.py` live
in `tests/<package>/`, usually as `test_<module>.py`, and tests for a top-level module such as
`wisp.retry` live directly in `tests/`. Other kinds of test have their own directories:

| Directory | Contents |
| -- | -- |
| `tests/tui_e2e/` | Drive the built `wisp-tui` binary over a pseudo-terminal; skipped unless `RUST_TUI_BINARY_UNDER_TEST` is set |
| `tests/benchmarks/` | Timing and resource-bound checks (`benchmark` marker) |
| `tests/docs/` | Keep documentation, the book's examples, and SDK examples in sync with the code |
| `tests/scripts/` | Tests for the helpers in `scripts/` |
| `tests/repository/` | Repository-wide rules: architecture boundaries, versions, fuzz corpus, test isolation |
| `tests/support/` | Shared helpers imported by tests; not collected |
| `tests/fixtures/` | Static fixture files |

## Test selection

CI splits the suite into two jobs that run side by side; use the same commands when triaging:

```bash
uv run pytest tests -m 'not (slow or process or benchmark)' -n auto
uv run pytest tests -m 'slow or process or benchmark'
```

Process, slow, and benchmark tests assert timing and resource bounds that flake when they compete
with parallel workers, so they run one at a time.

Markers are declared in `pyproject.toml`: `tui`, `process`, `benchmark`, `slow`, and
`production_fault`. TUI, benchmark, and production-fault files declare their relevant markers via
`pytestmark`. Mark `process` only on tests that start a real OS process, or on the whole file when
every test in it does; tests that fake the process layer belong in the parallel subset.

## Isolation

`tests/conftest.py` has an autouse fixture that clears every `WISP_*` environment variable and
repoints `HOME` and the working directory to temporary directories for each test. Tests opt into
configuration explicitly, so a local `~/.wisp` config can never affect results. If a test needs
trust, set it via `monkeypatch.setenv`.

Prefer `ScriptedProvider` / `FakeProvider` from `wisp.providers.fake` for new provider-facing tests
rather than live models.

## Harness interruption and recovery

For changes to conversation orchestration, start with:

```bash
uv run pytest tests/agent/harness tests/agent/invariant_checks/test_live_runs.py tests/coding
```

The interruption matrix records a normal event sequence for streaming, sequential and parallel
tools, queues, transcript replacement, and context rebase. Each fresh run cancels or explicitly closes
the stream after one emitted event boundary. Failure notes name the scenario, action, event type,
and occurrence; a single pytest case exercises all boundaries for its scenario and action.

Cancellation must settle its event stream. Explicit closure cannot publish terminal events, so the
test instead checks retained state and continues the same harness. Both paths check output retention,
queue ordering, and tool-result repair without duplicates. Separate fault cases cover provider/tool
exceptions, boundary preparation failure, and rejection of a stale rebase.

These deterministic fixtures complement targeted in-flight cancellation tests; they do not enumerate
every task interleaving or replace provider-adapter tests. For the ownership and lifecycle contracts,
see [Agent runtime architecture](../architecture/agent-runtime.md).

## Rust workspace and handoff

Use the repository's pinned Rust 1.85.0 toolchain for the Rust protocol and TUI gates:

```bash
uv run python -m wisp.rpc.protocol_schema --check
cargo fmt --all --check
cargo check --workspace --all-targets --all-features
cargo clippy --workspace --all-targets --all-features -- -D warnings
cargo test --workspace --all-features
uv run pytest tests/cli/native_tui/test_rust_launcher.py tests/cli/native_tui/test_supervision.py
```

The cross-language PTY tests require a built binary and run on macOS and Linux:

```bash
cargo build -p wisp-tui
RUST_TUI_BINARY_UNDER_TEST="$(pwd)/target/debug/wisp-tui" \
  uv run pytest tests/tui_e2e
```

`RUST_TUI_BINARY_UNDER_TEST` belongs only to this test harness. It is not launcher configuration and
must not be documented as a normal way to run Wisp; source launches use the absolute
`WISP_RUST_TUI_BINARY` override instead. Without the test-only variable, these tests skip.

For visual review, an ignored Rust test renders the shared synthetic conversation
(`tests/fixtures/tui_visual_conversation.json`) through the production draw path. For nine states
(conversation, tools, working, tool running and dimmed, approval, permissions, new turn, scrollback)
at 100 × 30, 80 × 24, 40 × 16 and 30 × 8, in dark, light and monochrome, it writes one JSON terminal
buffer per frame: every cell's text, colors and attributes.

```bash
WISP_VISUAL_OUTPUT=/tmp/wisp-tui-captures \
  cargo test -p wisp-tui capture_conversation_screens -- --ignored
```

Regenerate captures when you need them rather than committing them: they go stale with every UI
change, while geometry and style regressions are already checked by Rust buffer tests.

## CI policy

CI runs for pull requests targeting `main` or `develop`, for direct updates to `main`, and by manual
dispatch.

CI selects jobs from the complete Git diff: pull requests use the merge base, and pushes use
`before..after`. Renames include both paths. Manual runs, missing comparison commits, unknown paths,
CI changes, dependencies, schemas, and shared fixtures run all checks.

| Change | Checks in the CI workflow |
| -- | -- |
| Documentation site or assets only | Documentation and repository contract tests |
| README | Contract tests and package build/installed SDK verification |
| Rust TUI source only | Linux/macOS Rust workspace, Linux TUI handoff, contract tests |
| Python runtime | Quality, both Python suites, macOS platform smoke, package, TUI handoff |
| RPC/events, shared inputs, native crates, or unclassified changes | Full CI |

Mixed changes take the union of their checks. Contract tests run separately only when the full
Python suite is not selected; the full suite already includes them. Documentation builds and
Astro browser checks retain their own workflow path filters.

Linux is authoritative for the complete locked-environment Python quality and test suite: Ruff
formatting and lint, configured `uv run mypy`, live RPC schema verification, and the `tests/` suite.
Rust formatting runs on Linux; Clippy and workspace tests run on both Linux and macOS because
platform-specific code needs coverage on each. The TUI handoff and benchmark harnesses run on Linux
for relevant PRs; main and manual runs also exercise macOS handoff as a nonblocking diagnostic.

The `CI passed` job is the only required status check. It always runs and requires successful check
selection, every selected job to succeed, and every unselected job to be explicitly skipped. A
failed, cancelled, missing, or unexpectedly skipped job cannot make it pass. Selection and status
validation live in `scripts/ci_jobs.py`; their dependency-free tests run before job selection:

```bash
python3 -m unittest discover -s tests/scripts -p test_ci_jobs.py
```

Astro uses a pinned Playwright container with browsers and OS dependencies already installed, so
browser checks do not depend on fresh Ubuntu package downloads. Update both container images and
the build job's version check in `.github/workflows/docs.yml` with `@playwright/test` in
`site/package-lock.json`.

The reusable `Rust TUI wheel candidates` workflow builds `wisp-ai` platform wheels for manylinux
x86_64 and macOS arm64. It compares Python package files with the current `uv_build` wheel, verifies
the native extension and executable, and installs without Cargo on the consumer `PATH`. Each target
exercises an installed fake-provider Rust TUI prompt, the managed-output extension, native/pure
replacement with an actionable missing-binary error, corruption, offline reinstall, and uninstall;
it uploads checksums, a CycloneDX SBOM, and observed size/startup/RSS evidence.

It runs on runtime pushes to `main`, skipping changes confined to site content, assets, agent
instructions, and the changelog. Pull requests build candidates for native crates, shared build and
protocol inputs, packaging files, wheel scripts/tests, and Python modules that load a native binary
(the TUI launcher and the search and shell tools). TUI-only PRs use workspace and handoff checks;
their merged code still gets an installed-wheel build. Pull requests, `main` pushes, and manual runs
only upload candidates. The tag-gated release workflow calls the same reusable builder, verifies the
complete downloaded distribution set, and requires provenance attestation before trusted
publication.

The `production_fault` tests are a deterministic regression contract that runs with the rest of
the suite; to run them alone:

```bash
uv run pytest tests -m production_fault --durations=20
```

That contract inventories provider streams truncated before native completion, partial session and
auth writes, stale session writers, cancellation during SDK shutdown, and bounded process-tree
cleanup.

A focused macOS job covers auth/session locking and durability, subprocess and MCP cleanup, RPC/stdin
transport, secure filesystem operations, and a fake-provider CLI smoke test. The complete suite is
not duplicated on macOS because the remaining tests exercise platform-neutral contracts. Windows
remains best-effort until it has dedicated CI coverage.
