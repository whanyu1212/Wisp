# Testing

```bash
uv run pytest tests                                                  # complete suite
uv run pytest tests -m 'not (slow or process or benchmark)' -n auto  # quick subset
```

The complete suite runs against deterministic fake or scripted providers, so the agent core, CLI, and
JSONL sessions are exercised without API keys, live model calls, or provider credentials. Run the
complete command before considering a change verified.

## Test selection

CI splits the suite into two jobs that run side by side; use the same commands when triaging:

```bash
uv run pytest tests -m 'not (slow or process or benchmark)' -n auto
uv run pytest tests -m 'slow or process or benchmark'
```

Process, slow, and benchmark tests assert timing and resource bounds that flake when they compete
with parallel workers, so they run one at a time.

Markers are declared in `pyproject.toml`: `tui`, `process`, `benchmark`, `slow`, and
`production_fault`. TUI, process, benchmark, and production-fault files declare their relevant
markers via `pytestmark`.

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
uv run pytest tests/agent/test_agent_harness.py tests/agent/test_agent_harness_interruptions.py \
  tests/agent/test_agent_runtime_invariants.py tests/coding/test_coding_session.py tests/agent/test_compaction.py
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
uv run pytest tests/rust_tui/test_rust_tui_launcher.py tests/rust_tui/test_rust_tui_supervision.py
```

The cross-language PTY smoke test requires a built binary and runs on macOS and Linux:

```bash
cargo build -p wisp-tui
RUST_TUI_BINARY_UNDER_TEST="$(pwd)/target/debug/wisp-tui" \
  uv run pytest tests/rust_tui/test_rust_tui_smoke.py
```

`RUST_TUI_BINARY_UNDER_TEST` belongs only to this test harness. It is not launcher configuration and
must not be documented as a normal way to run Wisp; source launches use the absolute
`WISP_RUST_TUI_BINARY` override instead. Without the test-only variable, the smoke test skips.

## CI policy

CI runs for pull requests targeting `main` or `develop`, for direct updates to `main`, and by manual
dispatch.

Linux is authoritative for the complete locked-environment quality and test suite: Ruff formatting
and lint, configured `uv run mypy`, and the `tests/` suite.

The `CI passed` job is the only required status check. It succeeds only when every other job in the
CI workflow succeeds, so adding or renaming a job never needs a repository settings change.

A separate Rust workspace job runs the schema check, Rust formatting, check, Clippy, workspace tests,
build, and cross-language handoff smoke test on both Linux and macOS.

The reusable `Rust TUI wheel candidates` workflow builds `wisp-ai` platform wheels for manylinux
x86_64 and macOS arm64. It compares Python package files with the current `uv_build` wheel, verifies
the native extension and executable, and installs without Cargo on the consumer `PATH`. Each target
exercises an installed fake-provider Rust TUI prompt, the managed-output extension, native/pure
replacement with an actionable missing-binary error, corruption, offline reinstall, and uninstall;
it uploads checksums, a CycloneDX SBOM, and observed size/startup/RSS evidence.

It runs on every push to `main`, and on pull requests only when they change Rust crates, packaging
files, the wheel scripts or tests, or the Python modules that load a native binary (the TUI
launcher and the search and shell tools). Pull requests, `main` pushes, and manual workflow runs
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
