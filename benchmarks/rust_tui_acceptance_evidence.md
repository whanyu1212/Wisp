# Rust TUI acceptance evidence (#470)

Decision recorded 2026-09-03: Textual remains the default and supported frontend. Rust remains
experimental opt-in (rollout stage 2) on macOS and Linux. Explicit Rust selection does not fall back
to Textual. No shipped binary. No default switch. No Textual deprecation.

This file records which #470 measurements reached a terminal result and which were inaccessible.
Skipped measurements are not passes.

## Comparison policy

- Compare identical Wisp/backend versions, fixtures, viewport, and machine when both renderers are
  measured.
- Report individual samples and medians; do not promote one machine's timings to CI thresholds.
- Do not use network-provider latency as renderer evidence.
- Do not collect real prompts, tool payloads, paths, credentials, or session identifiers.
- Pending, timed-out, skipped-without-rationale, or inaccessible results are not passes.

## Environment

| Field | Value |
| --- | --- |
| Date | 2026-09-03 |
| Base tree | `1a57164b6b72d82d45b0748c6fcc295dddb339c0` |
| Worktree | Dirty with #470 documentation and tests; Rust transcript sources unchanged |
| Host | Mac14,15, Apple M2, arm64 |
| OS | macOS 26.6.2 / Darwin 25.6.0 |
| Rust | `rustc 1.85.0 (4d91de4e4 2025-02-17)` |
| Python | 3.12.2 |

## Commands that reached a terminal result

### Rust in-process transcript

```bash
mkdir -p profiles
cargo +1.85.0 run --release -p wisp-tui \
  --features transcript-benchmark --example transcript_benchmark -- \
  --entries 1000,10000,100000 --runs 5 \
  --output profiles/rust-tui-transcript.json
```

Exit 0. Medians and limits are in `benchmarks/rust_tui_transcript_evidence.md`. Raw JSON remains
ignored under `profiles/`. This is `TestBackend` frame construction, not PTY paint or Textual
comparison. It does not support a claim that Rust materially improves interactive p95/max versus
Textual.

### Shared transition traces

```bash
uv run pytest tests/test_tui_traces.py
cargo test -p wisp-tui --test tui_traces --all-features
```

These are the existing #459 conformance suites. They do not replace #468 hardening.

### Launcher and no-fallback

```bash
uv run pytest tests/test_rust_tui_launcher.py tests/test_rust_tui_supervision.py
```

These lock Textual as the default, reject a missing or non-executable Rust binary, and keep explicit
Rust failure from selecting Textual.

### Smoke

```bash
cargo build -p wisp-tui
RUST_TUI_BINARY_UNDER_TEST="$(pwd)/target/debug/wisp-tui" \
  uv run pytest tests/test_rust_tui_smoke.py
```

Exit 0: 5 passed in 8.68s. Without `RUST_TUI_BINARY_UNDER_TEST` the same file skips (5 skipped);
that skip is not a pass.

## Inaccessible measurements

| Required #470 evidence | Disposition | Owner |
| --- | --- | --- |
| Comparative idle/streaming input-to-frame p50/p95/p99/max for both renderers | Inaccessible. `benchmarks/tui_input_latency.py` is Textual-only. The Rust transcript bench is in-process Ratatui. No dual-renderer PTY harness exists. | Stop condition against a default switch. Not claimed. |
| End-to-end serialization + pipe + Rust validation + visible first-token | Inaccessible in this decision PR. | [#270](https://github.com/whanyu1212/Wisp/issues/270), [#468](https://github.com/whanyu1212/Wisp/issues/468) |
| Platform artifacts, install, upgrade, downgrade, fallback paths | Inaccessible. Python wheels do not contain `wisp-tui`. | [#469](https://github.com/whanyu1212/Wisp/issues/469) |
| Adversarial fuzz, backpressure, terminal injection, panic/PTY restore | Inaccessible as a complete #470 pass. Existing supervision tests are not that suite. | [#468](https://github.com/whanyu1212/Wisp/issues/468) |
| Maintainer/user opt-in cohort feedback | None. There is no public opt-in rollout. Empty on purpose, not a pass. | Stage 3, after [#469](https://github.com/whanyu1212/Wisp/issues/469) |

## Recommendation

Keep Textual as default. Keep Rust at experimental opt-in. Do not file a default-switch issue. Do
not file a Textual-deprecation issue. Stage 3 remains blocked on
[#467](https://github.com/whanyu1212/Wisp/issues/467),
[#468](https://github.com/whanyu1212/Wisp/issues/468), and
[#469](https://github.com/whanyu1212/Wisp/issues/469).
