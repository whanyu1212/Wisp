---
title: Testing
---

# Testing

```bash
uv run pytest tests                                                  # complete suite
uv run pytest tests -m 'not (slow or tui or process or benchmark or production_fault)'  # core
```

The complete suite runs against deterministic fake or scripted providers, so the agent core, CLI, and
JSONL sessions are exercised without API keys, live model calls, or provider credentials. Run the
complete command before considering a change verified.

## Test selection

CI splits the suite into four marker-based jobs; mirror the matrix predicates exactly when
triaging:

```bash
uv run pytest tests -m 'not (slow or tui or process or benchmark or production_fault)'
uv run pytest tests -m 'tui and not production_fault'
uv run pytest tests -m '(slow or process or benchmark) and not (tui or production_fault)'
uv run pytest tests -m 'production_fault'
```

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

## Rust workspace and handoff

Use the repository's pinned Rust 1.85.0 toolchain for the Rust protocol and TUI scaffold gates:

```bash
uv run python -m wisp.rpc.protocol_schema --check
cargo fmt --all --check
cargo check --workspace --all-targets --all-features
cargo clippy --workspace --all-targets --all-features -- -D warnings
cargo test --workspace --all-features
uv run pytest tests/test_rust_tui_launcher.py tests/test_rust_tui_supervision.py
```

The cross-language PTY smoke test requires a built binary and runs on macOS and Linux:

```bash
cargo build -p wisp-tui
RUST_TUI_BINARY_UNDER_TEST="$(pwd)/target/debug/wisp-tui" \
  uv run pytest tests/test_rust_tui_smoke.py
```

`RUST_TUI_BINARY_UNDER_TEST` belongs only to this test harness. It is not launcher configuration and
must not be documented as a normal way to run Wisp; source launches use the absolute
`WISP_RUST_TUI_BINARY` override instead. Without the test-only variable, the smoke test skips.

## CI policy

CI runs for pull requests targeting `main` or `develop`, for direct updates to `main`, and by manual
dispatch.

Linux is authoritative for the complete locked-environment quality and test suite: Ruff formatting
and lint, configured `uv run mypy`, and `tests/`-only pytest partitions.

A separate Rust workspace job runs the schema check, Rust formatting, check, Clippy, workspace tests,
build, and cross-language handoff smoke test on both Linux and macOS.

The `production_fault` partition is a required deterministic regression contract:

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

CI additionally sets `WISP_TRUST=1`, `WISP_EFFORT=xhigh`, `WISP_CONTEXT_RESERVE_TOKENS=4096`, and
`WISP_AUTO_COMPACTION=0`. Match these if a test passes locally but fails in CI.
