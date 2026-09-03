# Rust TUI transcript performance evidence

Refreshed 2026-09-03 for issue #470. The previous snapshot was 2026-08-29 for #465. Raw JSON and
sampling output remain ignored under `profiles/`.

This is not a comparative Textual measurement and does not authorize a default-renderer change.
See `benchmarks/rust_tui_acceptance_evidence.md`.

## Reproducibility

| Field | Value |
| --- | --- |
| Base tree | `1a57164b6b72d82d45b0748c6fcc295dddb339c0` |
| Worktree | Dirty with #470 documentation and tests; Rust transcript sources unchanged |
| Host | Mac14,15, Apple M2, arm64 |
| OS | macOS 26.6.2 / Darwin 25.6.0 |
| Rust | `rustc 1.85.0 (4d91de4e4 2025-02-17)` |
| Build | `--release`, 100x24 viewport, five rotated runs |
| Conditions | 1,000 / 10,000 / 100,000 transcript entries; identical 512-entry rich suffix |

Command:

```bash
cargo +1.85.0 run --release -p wisp-tui \
  --features transcript-benchmark --example transcript_benchmark -- \
  --entries 1000,10000,100000 --runs 5 \
  --output profiles/rust-tui-transcript.json
```

## Results

Values are medians across the five per-condition samples. Warm, navigation, resize, stream, and
paged-detail values are medians of each sample's p95 distribution. Maximum synchronous stall is the
largest observed value across the five samples; the other columns are medians.

| Entries | Cold frame ms | Warm p95 ms | Navigation p95 ms | Resize p95 ms | Stream p95 ms | Stream CPU / 100 updates ms | Detail open ms | Detail p95 ms | Max synchronous stall ms |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | 0.217 | 0.093 | 0.105 | 0.107 | 0.144 | 11.913 | 0.207 | 0.109 | 0.556 |
| 10,000 | 0.246 | 0.095 | 0.106 | 0.104 | 0.141 | 11.895 | 0.202 | 0.119 | 0.360 |
| 100,000 | 0.254 | 0.105 | 0.114 | 0.105 | 0.138 | 11.776 | 0.203 | 0.114 | 0.315 |

The first syntax-highlight call took 17.995 ms in this run. This includes Syntect initialization,
parsing, and highlighting the first fence; it is reported separately from warm stream frames and has
no portable threshold.

100k / 1k ratios:

| Metric | Ratio |
| --- | ---: |
| Warm p95 | 1.126 |
| Navigation p95 | 1.084 |
| Resize p95 | 0.986 |
| Stream p95 | 0.961 |
| Stream process CPU | 0.988 |
| Detail open | 0.982 |
| Detail p95 | 1.040 |

All ratios are below the 1.25 same-machine scaling gate. Warm, navigation, resize, stream, detail
open, and detail p95 values are below 16 ms; measured maximum synchronous stalls are below 32 ms.

## Deterministic evidence

- `scaling_work_independent`: `true` for cold, warm, navigation, resize, and stream work.
- `all_correctness_checks_passed`: `true` for all 15 samples.
- Warm draws built zero rows and parsed/highlighted zero source bytes.
- Streaming retained exact source, stayed at the tail without unseen output, reused stable Markdown,
  performed no full reparse, and stayed within the configured parse/highlight work bounds.
- Visible rows remained viewport-bounded; the structured diff saturated the 400-row retention
  budget, rendered known old/new rows through its bounded cache, included eager row formatting plus
  the first detail draw in both `detail_open_ms` and maximum synchronous stall, and reached the
  retained tail after 23 measured page-down frames in every sample.
- A default-suite regression independently compares 1,000 and 100,000-entry transcripts without
  using timing assertions.

## Profile

The 2026-08-29 two-second macOS `sample` capture was not repeated for #470. That capture observed
500 repeated, correctness-passing 100k-entry workloads. The most frequent production draw samples
were Ratatui line reflow/grapheme traversal and buffer diffing. The refreshed 2026-09-03 timings
remain flat across transcript length, so that profile is not treated as a current hotpath.

## Limits

- `TestBackend` measures in-process Ratatui frame construction and diffing, not PTY writes, emulator
  paint, perceptual latency, or event-loop scheduling.
- Absolute timings apply only to this machine and build. CI asserts deterministic work and
  correctness, never these timings.
- This is not a comparative Textual or PTY input-to-frame measurement.
- The ignored raw JSON lives under `profiles/` and is not committed.
